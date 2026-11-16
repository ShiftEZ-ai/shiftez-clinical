"""Events share M1 delivery evidence without coupling receipts to inbox visibility."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event as sql_event, func, insert, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services import event_delivery
from tests.outbox_support import process_one_after_undo as process_one


def scheduled(h, *, notify=True, attendance="expected", audience=None, occurrences=1):
    with transaction(h.app.state.engine, write=True) as db:
        kind = m.EventType(department_id=h.seed["department_id"], name="Synthetic event type",
                           starts_on=date(2020, 1, 1), notify_inbox=notify, attendance=attendance)
        db.add(kind)
        db.flush()
        start = datetime(2026, 10, 4, 7, tzinfo=timezone.utc)
        row = m.Event(department_id=kind.department_id, event_type_id=kind.id,
                      heading="Synthetic event", description="Synthetic agenda", start=start,
                      end=start + timedelta(hours=1), place="Synthetic room", audience=audience or {"roles": ["staff"]})
        db.add(row)
        db.flush()
        saved = []
        for index in range(occurrences):
            at = start + timedelta(days=index)
            occurrence = m.EventOccurrence(department_id=row.department_id, event_id=row.id,
                original_start=at, start=at, end=at + timedelta(hours=1),
                data={**record(row), "type": record(kind)})
            db.add(occurrence)
            saved.append(occurrence)
        db.flush()
        event_delivery.queue(db, SimpleNamespace(department_id=row.department_id,
            identity_id=h.seed["identities"]["coordinator"]), row)
        return row.id, [item.id for item in saved]


def notes(h):
    return [item for item in h.get("staff", "/api/notifications")["notifications"] if item["source_kind"] == "event"]


def own_receipt(h, occurrence_id):
    with transaction(h.app.state.engine) as db:
        return record(db.scalar(select(m.AnnouncementReceipt).where(
            m.AnnouncementReceipt.event_occurrence_id == occurrence_id,
            m.AnnouncementReceipt.identity_id == h.seed["identities"]["staff"])))


def test_revision_delivery_is_one_notification_and_receipts_for_every_occurrence(h):
    event_id, occurrences = scheduled(h, occurrences=3)
    process_one(h.app.state.engine)
    first = notes(h)
    assert len(first) == 1 and first[0]["source_id"] == occurrences[0]
    receipts = [own_receipt(h, occurrence_id) for occurrence_id in occurrences]
    assert all(item["delivered_at"] and item["read_at"] is None for item in receipts)
    process_one(h.app.state.engine)
    assert notes(h) == first
    assert [own_receipt(h, occurrence_id) for occurrence_id in occurrences] == receipts
    with transaction(h.app.state.engine) as db:
        outbox = list(db.scalars(select(m.Outbox).where(m.Outbox.kind == "event")))
        assert len(outbox) == 1 and outbox[0].event_key == f"event:{event_id}:1"


def test_superseded_revision_is_skipped_and_current_revision_delivers(h):
    event_id, occurrences = scheduled(h)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Event, event_id)
        row.revision += 1
        event_delivery.queue(db, SimpleNamespace(department_id=row.department_id,
            identity_id=h.seed["identities"]["coordinator"]), row, occurrences)
    process_one(h.app.state.engine)
    assert len(notes(h)) == 1
    with transaction(h.app.state.engine) as db:
        outboxes = list(db.scalars(select(m.Outbox).where(m.Outbox.kind == "event").order_by(m.Outbox.created_at)))
        assert len(outboxes) == 2 and all(item.state == "available_in_app" for item in outboxes)
        assert all("occurrence_ids" not in item.payload for item in outboxes)
        stale = next(item for item in outboxes if item.payload["revision"] == 1)
        assert db.scalar(select(func.count()).select_from(m.Notification).where(m.Notification.outbox_id == stale.id)) == 0


def test_latest_revision_carries_rapid_changes_to_distinct_occurrences(h):
    event_id, occurrences = scheduled(h, occurrences=2)
    with transaction(h.app.state.engine, write=True) as db:
        second = db.get(m.EventOccurrence, occurrences[1])
        second.data = {**second.data, "audience": {"roles": ["coordinator"]}}
    process_one(h.app.state.engine)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Event, event_id)
        actor = SimpleNamespace(department_id=row.department_id, identity_id=h.seed["identities"]["coordinator"])
        for occurrence_id in occurrences:
            occurrence = db.get(m.EventOccurrence, occurrence_id)
            occurrence.data = {**occurrence.data, "description": "Updated synthetic agenda"}
            row.revision += 1
            event_delivery.queue(db, actor, row, [occurrence_id])
        db.flush()
        latest = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f"event:{event_id}:3"))
        assert set(latest.payload["occurrence_ids"]) == set(occurrences)
    process_one(h.app.state.engine)
    assert len(notes(h)) == 2
    assert any(item["message"] == "Updated synthetic agenda" for item in notes(h))


def test_inbox_off_receipt_can_be_acknowledged_and_keeps_rsvp(h):
    _, occurrences = scheduled(h, notify=False, attendance="must_acknowledge")
    process_one(h.app.state.engine)
    assert notes(h) == []
    original = own_receipt(h, occurrences[0])
    assert original["delivered_at"] is None
    with transaction(h.app.state.engine, write=True) as db:
        receipt = db.get(m.AnnouncementReceipt, original["id"])
        receipt.response, receipt.responded_at = "going", m.now()
    result = h.command("staff", "notification.acknowledge", {"reason": "Explicit synthetic acknowledgement"},
                       original["id"], original["revision"])["receipt"]
    assert result["read_at"] and result["acknowledged_at"]
    assert result["response"] == "going" and result["responded_at"]
    assert result["delivered_at"] is None


def test_read_and_mark_all_do_not_acknowledge_event(h):
    _, occurrences = scheduled(h, attendance="must_acknowledge")
    process_one(h.app.state.engine)
    note = notes(h)[0]
    h.command("staff", "notification.ack", {"reason": "Opened"}, note["id"], note["revision"])
    opened = own_receipt(h, occurrences[0])
    assert opened["read_at"] and opened["acknowledged_at"] is None
    h.command("staff", "notification.ack_all", {"reason": "Read inbox"})
    assert own_receipt(h, occurrences[0])["acknowledged_at"] is None
    note = notes(h)[0]
    h.command("staff", "notification.acknowledge", {"reason": "Explicit acknowledgement"}, note["id"], note["revision"])
    assert own_receipt(h, occurrences[0])["acknowledged_at"]


def test_acknowledgement_refuses_cancelled_and_no_longer_matching_occurrences(h):
    _, occurrences = scheduled(h, attendance="must_acknowledge")
    process_one(h.app.state.engine)
    receipt = own_receipt(h, occurrences[0])
    with transaction(h.app.state.engine, write=True) as db:
        occurrence = db.get(m.EventOccurrence, occurrences[0])
        occurrence.data = {**occurrence.data, "audience": {"roles": ["coordinator"]}}
    h.command("staff", "notification.acknowledge", {"reason": "Old receipt"}, receipt["id"], receipt["revision"], status=404)
    with transaction(h.app.state.engine, write=True) as db:
        occurrence = db.get(m.EventOccurrence, occurrences[0])
        occurrence.data = {**occurrence.data, "audience": {"roles": ["staff"]}}
        occurrence.cancelled = True
    h.command("staff", "notification.acknowledge", {"reason": "Cancelled occurrence"}, receipt["id"], receipt["revision"], status=409)
    assert own_receipt(h, occurrences[0])["acknowledged_at"] is None


def test_cancellation_reaches_prior_audience_and_preserves_receipt(h):
    event_id, occurrences = scheduled(h)
    process_one(h.app.state.engine)
    original = own_receipt(h, occurrences[0])
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Event, event_id)
        row.revision += 1
        occurrence = db.get(m.EventOccurrence, occurrences[0])
        occurrence.data = {**occurrence.data, "audience": {"roles": ["coordinator"]}}
        occurrence.cancelled = True
        event_delivery.queue(db, SimpleNamespace(department_id=row.department_id,
            identity_id=h.seed["identities"]["coordinator"]), row, [occurrence.id])
    process_one(h.app.state.engine)
    delivered = notes(h)
    assert len(delivered) == 2 and any(item["title"].startswith("Cancelled:") for item in delivered)
    assert own_receipt(h, occurrences[0]) == original


@pytest.mark.postgres
def test_150_person_delivery_query_count_is_constant(pg):
    engine = pg.app.state.engine
    with transaction(engine, write=True) as db:
        people = [{"id": m.uid(), "organisation_id": pg.seed["organisation_id"], "display_name": f"Event person {i}"}
                  for i in range(150)]
        identities = [{"id": m.uid(), "issuer": "local-development", "subject": f"event-scale-{i}",
                       "display_name": person["display_name"], "person_id": person["id"]} for i, person in enumerate(people)]
        db.execute(insert(m.Person), people)
        db.execute(insert(m.Identity), identities)
        db.execute(insert(m.RoleBinding), [{"identity_id": item["id"], "department_id": pg.seed["department_id"],
            "role": "staff", "starts_on": date(2020, 1, 1)} for item in identities])
    statements = []

    def count_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sql_event.listen(engine, "before_cursor_execute", count_statement)
    try:
        _, one = scheduled(pg, audience={"person_ids": [people[0]["id"]], "everyone": False})
        process_one(engine)
        small = len(statements)
        statements.clear()
        _, many = scheduled(pg, audience={"person_ids": [person["id"] for person in people], "everyone": False})
        process_one(engine)
        large = len(statements)
    finally:
        sql_event.remove(engine, "before_cursor_execute", count_statement)
    with transaction(engine) as db:
        for ids, expected_count in ((one, 1), (many, 150)):
            assert db.scalar(select(func.count()).select_from(m.AnnouncementReceipt).where(
                m.AnnouncementReceipt.event_occurrence_id.in_(ids), m.AnnouncementReceipt.delivered_at.is_not(None))) == expected_count
    assert large <= small + 8, (small, large)
