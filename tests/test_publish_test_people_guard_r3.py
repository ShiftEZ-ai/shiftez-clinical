"""Source authors bind system delivery and every shared announcement/event reader."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event as sql_event, select

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.services import announcements, audiences, events
from apps.api.services import pwa
from apps.worker import email_delivery
from tests.clock_support import freeze_clock
from tests.governance_support import identity
from tests.outbox_support import dispatch_batch_after_undo
from tests.test_email_delivery import configuration, no_account_person
from tests.test_events import event as calendar_event, input_of, repeat
from tests.test_publish_test_people_guard import enabled, rename
from tests.test_pwa_push import ready, subscribe


pytestmark = pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)


def notice(h, *, role="coordinator", **changes):
    return h.configure("department_content", {"name": "Synthetic privacy announcement",
        "description": "Synthetic private agenda", "starts_on": "2020-01-01", "kind": "announcement",
        "audience": {"everyone": True}, "notify": True, **changes}, role=role)["record"]


@pytest.mark.parametrize("path", ["announcement_reminder", "actorless_event", "event_extension"])
def test_system_delivery_uses_source_creator_for_every_channel(ready, monkeypatch, path):
    h = ready
    enabled(h, True)
    configuration(h)
    h.command("admin", "notification.policy.update", {"department_id": h.seed["department_id"],
        "push_enabled": True, "event_defaults": {kind: {"email": "on", "push": "on"}
            for kind in ("announcement", "event")}, "reason": "Synthetic source privacy"}, revision=2)
    author = h.seed["identities"]["coordinator"]
    rename(h, author, "Synthetic author (test)")
    rename(h, h.seed["identities"]["staff"], "Synthetic real recipient", h.seed["people"][0])
    rename(h, h.seed["identities"]["approver"], "Synthetic test recipient (test)")
    for role in ("staff", "approver"):
        subscribe(h, role, endpoint="https://fcm.googleapis.com/source-privacy-" + role, hint="privacy-" + role)
    email_person = no_account_person(h)
    if path == "announcement_reminder":
        row = notice(h, also_email=True)
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Department, h.seed["department_id"]).announcement_auto_reminder_days = 1
            announcements.reconcile(db)
            source = db.scalar(select(m.Outbox).where(m.Outbox.kind == "announcement",
                m.Outbox.payload["reminder"].as_boolean().is_(True)))
            assert source is not None and source.payload["actor_id"] is None
            source_id = source.id
    else:
        changes = {"audience": {"everyone": True}, "also_email": True}
        if path == "event_extension":
            changes.update(repeat=repeat(), repeat_until="2026-10-18")
            with transaction(h.app.state.engine, write=True) as db:
                db.get(m.Department, h.seed["department_id"]).event_horizon_days = 7
        row = calendar_event(h, **changes)
        with transaction(h.app.state.engine, write=True) as db:
            if path == "event_extension":
                freeze_clock(monkeypatch, m.now() + timedelta(days=3))
                events.reconcile(db)
                source = db.scalar(select(m.Outbox).where(m.Outbox.kind == "event",
                    m.Outbox.event_key.contains(":window:")))
                assert source is not None and source.payload["actor_id"] != author
            else:
                source = db.scalar(select(m.Outbox).where(m.Outbox.kind == "event",
                    m.Outbox.payload["event_id"].as_string() == row["id"]))
                source.payload = {key: value for key, value in source.payload.items() if key != "actor_id"}
            source_id = source.id
    with transaction(h.app.state.engine, write=True) as db:
        dispatch_batch_after_undo(db)
        allowed = {h.seed["identities"]["approver"], author}
        for model, column in ((m.Notification, m.Notification.outbox_id),
                              (m.EmailDelivery, m.EmailDelivery.source_outbox_id),
                              (m.PushDelivery, m.PushDelivery.source_outbox_id)):
            recipients = set(db.scalars(select(model.identity_id).where(column == source_id)))
            assert recipients and recipients <= allowed, (model.__name__, recipients)
        assert not db.scalar(select(m.EmailDelivery.id).where(
            m.EmailDelivery.source_outbox_id == source_id, m.EmailDelivery.person_id == email_person))
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.target_id == source_id,
            m.AuditEvent.action == "notification.test_people_only_suppressed"))
        assert audit and audit.actor_id == author and audit.detail["count"] > 0


def test_all_shared_readers_hide_test_sources_and_setting_off_restores_them(h):
    enabled(h, False)
    rename(h, h.seed["identities"]["coordinator"], "Synthetic author (test)")
    rename(h, h.seed["identities"]["staff"], "Synthetic real recipient", h.seed["people"][0])
    rename(h, h.seed["identities"]["approver"], "Synthetic test recipient (test)")
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["approver"]).person_id = h.seed["people"][1]
    identity(h, "real_coordinator", ["coordinator"])
    rename(h, h.seed["identities"]["real_coordinator"], "Synthetic real coordinator")
    content = notice(h, importance="must_acknowledge")
    event = calendar_event(h, audience={"everyone": True})
    real_content = notice(h, role="real_coordinator", name="Synthetic real announcement")
    real_event = h.configure("events", input_of(event, heading="Synthetic real event"), role="real_coordinator")["record"]
    with transaction(h.app.state.engine, write=True) as db:
        dispatch_batch_after_undo(db)
    feeds = {role: h.command(role, "calendar_feed.create")["path"] for role in ("staff", "approver")}

    def check(role, visible):
        content_ids = {r["id"] for r in h.get(role, "/api/content")["items"]}
        assert (content["id"] in content_ids) is visible
        assert real_content["id"] in content_ids
        home = h.get(role, "/api/home")
        assert any(item["key"] == "acknowledgements" and any(
            row["content_id"] == content["id"] for row in item["sample"]) for item in home["attention"]) is visible
        occurrences = h.get(role, "/api/events/occurrences?starts_on=2026-10-01&ends_on=2026-11-01")["occurrences"]
        assert (event["id"] in {r["event_id"] for r in occurrences}) is visible
        assert real_event["id"] in {r["event_id"] for r in occurrences}
        schedule = h.get(role, "/api/schedule/days?start=2026-10-01&end=2026-11-01")
        assert (event["id"] in {r["event_id"] for r in schedule["events"]}) is visible
        assert real_event["id"] in {r["event_id"] for r in schedule["events"]}
        notes = h.get(role, "/api/notifications")["notifications"]
        assert any(r["source_kind"] == "department_content" and r["source_id"] == content["id"] for r in notes) is visible
        assert any(r["link"].get("event_id") == event["id"] for r in notes) is visible
        if role in feeds:
            response = h.client(role).get(feeds[role])
            assert response.status_code == 200
            assert ("SUMMARY:Synthetic meeting\r\n" in response.text) is visible
        if role in {"coordinator", "real_coordinator"}:
            catalogues = h.get(role, "/api/catalogues?include=department_content&include=events")
            assert (content["id"] in {r["id"] for r in catalogues["department_content"]}) is visible
            assert (event["id"] in {r["id"] for r in catalogues["events"]}) is visible
            assert real_content["id"] in {r["id"] for r in catalogues["department_content"]}
            assert real_event["id"] in {r["id"] for r in catalogues["events"]}
            h.get(role, f"/api/events/definitions/{event['id']}", status=200 if visible else 404)
            h.get(role, f"/api/catalogues/department_content/{content['id']}/history", status=200 if visible else 404)
            h.get(role, f"/api/content/{content['id']}/email-preview", status=200 if visible else 404)

    # Warm the same-role cache with both unrestricted and test-author views.
    check("real_coordinator", True)
    path = "/api/catalogues?include=department_content&include=events"
    before = h.client("real_coordinator").get(path)
    enabled(h, True)
    refreshed = h.client("real_coordinator").get(path, headers={"if-none-match": before.headers["etag"]})
    assert refreshed.status_code == 200
    check("coordinator", True)
    check("real_coordinator", False)
    check("staff", False)
    check("approver", True)
    enabled(h, False)
    for role in ("staff", "approver", "coordinator", "real_coordinator"):
        check(role, True)


def test_visibility_query_count_is_constant_for_many_sources(h, record_property):
    enabled(h, True)
    rename(h, h.seed["identities"]["coordinator"], "Synthetic author (test)")
    notice(h)
    calendar_event(h, audience={"everyone": True})
    with transaction(h.app.state.engine, write=True) as db:
        staff = db.get(m.Identity, h.seed["identities"]["staff"])
        actor = Actor(staff.id, h.seed["department_id"], h.seed["organisation_id"], staff.display_name,
            frozenset({"staff"}), staff.person_id, "")
        statements = []

        def count(*args):
            statements.append(args[2])

        def read():
            statements.clear()
            query = audiences.visible_content(db, actor, date(2026, 10, 3), select(m.DepartmentContent).where(
                m.DepartmentContent.department_id == actor.department_id))
            notices = list(db.scalars(query))
            occurrences = events.viewer_occurrences(db, actor, date(2026, 10, 1), date(2026, 11, 1))
            assert notices == [] and occurrences == []
            return len(statements)

        sql_event.listen(h.app.state.engine, "before_cursor_execute", count)
        try:
            small = read()
            author = SimpleNamespace(identity_id=h.seed["identities"]["coordinator"],
                department_id=actor.department_id)
            from apps.api.services.common import history_many
            original = db.scalar(select(m.EventOccurrence))
            entries = []
            for i in range(150):
                row = m.DepartmentContent(department_id=actor.department_id, name=f"Synthetic notice {i}",
                    starts_on=date(2020, 1, 1))
                event = m.Event(department_id=actor.department_id, event_type_id=original.data["event_type_id"],
                    heading=f"Synthetic event {i}", start=original.start, end=original.end)
                db.add_all([row, event])
                db.flush()
                db.add(m.EventOccurrence(department_id=actor.department_id, event_id=event.id,
                    original_start=original.start, start=original.start, end=original.end, data=original.data))
                entries.extend([("department_content", row, "Synthetic scale"), ("events", event, "Synthetic scale")])
            history_many(db, author, entries)
            db.flush()
            large = read()
        finally:
            sql_event.remove(h.app.state.engine, "before_cursor_execute", count)
        record_property("visibility_statements_small", small)
        record_property("visibility_statements_151_sources", large)
        assert large <= small


def test_actorless_source_with_creator_column_is_filtered(h):
    from apps.api.services.notification_audience import filter_recipients
    enabled(h, True)
    author = h.seed["identities"]["coordinator"]
    rename(h, author, "Synthetic author (test)")
    rename(h, h.seed["identities"]["approver"], "Synthetic recipient (test)")
    with transaction(h.app.state.engine, write=True) as db:
        request = m.RequestRecord(department_id=h.seed["department_id"], person_id=h.seed["people"][0],
            start=m.now(), end=m.now() + timedelta(hours=1), private_reason="Synthetic request", created_by=author)
        db.add(request)
        db.flush()
        outbox = m.Outbox(department_id=request.department_id, kind="request", event_key="synthetic:actorless",
            payload={"request_id": request.id})
        db.add(outbox)
        db.flush()
        assert filter_recipients(db, outbox, {h.seed["identities"][role] for role in ("staff", "approver")}) == {
            h.seed["identities"]["approver"]}


@pytest.mark.parametrize("kind", ["announcement", "event"])
def test_pending_channel_children_recheck_source_privacy(ready, kind):
    h = ready
    enabled(h, False)
    configuration(h)
    rename(h, h.seed["identities"]["coordinator"], "Synthetic author (test)")
    email_person = no_account_person(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, h.seed["people"][0]).work_email = "synthetic-person@example.test"
    if kind == "announcement":
        notice(h, also_email=True)
    else:
        calendar_event(h, audience={"everyone": True}, also_email=True)
    with transaction(h.app.state.engine, write=True) as db:
        source = db.scalar(select(m.Outbox).where(m.Outbox.kind == kind))
        source.payload = {key: value for key, value in source.payload.items() if key != "actor_id"}
        source_id = source.id
    with transaction(h.app.state.engine, write=True) as db:
        dispatch_batch_after_undo(db)
    enabled(h, True)
    with transaction(h.app.state.engine) as db:
        source = db.get(m.Outbox, source_id)
        staff = h.seed["identities"]["staff"]
        note = db.scalar(select(m.Notification).where(m.Notification.outbox_id == source_id, m.Notification.identity_id == staff))
        assert note is not None and not pwa.notification_reaches(db, note)
        deliveries = list(db.scalars(select(m.EmailDelivery).where(m.EmailDelivery.source_outbox_id == source_id,
            (m.EmailDelivery.identity_id == staff) | (m.EmailDelivery.person_id == email_person))))
        assert {delivery.person_id for delivery in deliveries} == {h.seed["people"][0], email_person}
        for delivery in deliveries:
            with pytest.raises(email_delivery.DeliveryError, match="EMAIL_ACCESS_ENDED"):
                email_delivery._eligible(db, delivery, source, contexts={}, identity_ids={staff})
    enabled(h, False)
    with transaction(h.app.state.engine) as db:
        source = db.get(m.Outbox, source_id)
        note = db.scalar(select(m.Notification).where(m.Notification.outbox_id == source_id, m.Notification.identity_id == staff))
        assert pwa.notification_reaches(db, note)
        for delivery in db.scalars(select(m.EmailDelivery).where(m.EmailDelivery.source_outbox_id == source_id,
                (m.EmailDelivery.identity_id == staff) | (m.EmailDelivery.person_id == email_person))):
            email_delivery._eligible(db, delivery, source, contexts={}, identity_ids={staff})
