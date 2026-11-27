"""Inbox paging keeps histories complete and work bounded to the requested page."""
from datetime import timedelta
from time import perf_counter

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from apps.worker import main as worker
from packages.contracts.schemas import NotificationReadResult, NotificationRow
from .test_request_decide_many import request_row, REASON


@pytest.fixture(scope="module")
def inbox_scale(scale_harness):
    h = scale_harness()
    identity = h.seed["identities"]["staff"]
    with transaction(h.app.state.engine, write=True) as db:
        for n in range(250):
            outbox = m.Outbox(department_id=h.seed["department_id"], event_key=f"inbox-scale:{n}",
                             kind="request" if n % 2 else "swap", payload={})
            db.add(outbox)
            db.flush()
            db.add(m.Notification(outbox_id=outbox.id, identity_id=identity,
                title=f"Synthetic notification {n}", message="A synthetic message.",
                created_at=m.now() - timedelta(seconds=n), read_at=m.now() if n < 100 else None))
    h.client("staff")
    return h


def test_scale_inbox_filters_before_paging_with_two_bounded_queries(inbox_scale):
    statements, loaded = [], []
    def queried(conn, cursor, statement, parameters, context, executemany):
        if "from notifications" in statement.lower():
            statements.append(statement)
    def materialized(row, context):
        loaded.append(row.id)
    event.listen(inbox_scale.app.state.engine, "after_cursor_execute", queried)
    event.listen(m.Notification, "load", materialized)
    started = perf_counter()
    try:
        response = inbox_scale.get("staff", "/api/notifications?offset=25&limit=25&unread=true&kind=request")
    finally:
        event.remove(inbox_scale.app.state.engine, "after_cursor_execute", queried)
        event.remove(m.Notification, "load", materialized)
    elapsed = perf_counter() - started
    assert response["total"] == 75 and response["unread_total"] == 150
    assert response["limit"] == 25 and response["offset"] == 25
    assert len(response["notifications"]) == len(loaded) == 25
    assert all(note["read_at"] is None and note["kind"] == "request" for note in response["notifications"])
    # Deterministic CI budget: only the requested rows materialize and query count never grows with inbox history.
    assert len(statements) == 2
    pages = [inbox_scale.get("staff", f"/api/notifications?offset={offset}") for offset in (0, 100, 200)]
    assert [len(page["notifications"]) for page in pages] == [100, 100, 50]
    assert len({note["id"] for page in pages for note in page["notifications"]}) == 250
    assert inbox_scale.get("staff", "/api/notifications?unread=true&kind=request&offset=75")["notifications"] == []
    assert inbox_scale.get("staff", "/api/notifications?search=%25")["total"] == 0
    assert inbox_scale.get("staff", "/api/notifications?search=notification%20249")["total"] == 1
    measurement = {"returned_rows": len(response["notifications"]), "returned_unread_rows": 25,
        "filtered_total": response["total"], "unread_total": response["unread_total"],
        "notification_queries": len(statements), "materialized_rows": len(loaded), "seconds": elapsed}
    print(measurement)


def test_read_reply_contains_projected_changed_row_and_badge_without_list_reload(h):
    request_id = m.uid()
    with transaction(h.app.state.engine, write=True) as db:
        outbox = m.Outbox(department_id=h.seed["department_id"], event_key="read-reply", kind="request",
                         payload={"request_id": request_id})
        db.add(outbox)
        db.flush()
        note = m.Notification(outbox_id=outbox.id, identity_id=h.seed["identities"]["staff"],
                              title="Synthetic read patch", message="Message")
        db.add(note)
        db.flush()
        note_id = note.id
    reply = h.command("staff", "notification.ack", target=note_id, revision=1)
    NotificationReadResult.model_validate(reply)
    [changed] = reply["notifications"]
    assert changed["id"] == note_id and changed["revision"] == 2 and changed["read_at"]
    assert changed["kind"] == "request" and changed["link"]["request_id"] == request_id
    assert reply["unread_total"] == 0
    assert h.get("staff", "/api/notifications")["notifications"] == [
        NotificationRow.model_validate(changed).model_dump(mode="json")]
    h.command("coordinator", "notification.ack", target=note_id, revision=2, status=404)
    all_read = h.command("staff", "notification.ack_all")
    NotificationReadResult.model_validate(all_read)
    assert all_read["unread_total"] == 0


@pytest.mark.parametrize("query", ["offset=-1", "limit=101", "limit=0", "kind=" + "x" * 41, "search=" + "x" * 201])
def test_inbox_paging_and_filters_reject_unbounded_inputs(h, query):
    h.get("staff", "/api/notifications?" + query, status=422)


def test_delayed_request_notifications_describe_the_event_not_current_state(h):
    row = request_row(h)
    h.command("approver", "request.decide", {"decision": "approve", "reason": REASON}, row["id"], row["revision"])
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.Outbox).where(m.Outbox.payload["request_id"].as_string() == row["id"])
                                .order_by(m.Outbox.created_at, m.Outbox.event_key)))
        recipient = db.get(m.Identity, h.seed["identities"]["coordinator"])
        wording = [worker.describe(db, item, None, recipient) for item in events]
    assert {item[0] for item in wording} == {"New leave request from Staff 01(test)", "Staff 01(test)'s leave request was approved"}
    assert all("Open Requests for the details." not in item[1] for item in wording)
    assert {item.payload["request_status"] for item in events} == {"submitted", "approved"}
