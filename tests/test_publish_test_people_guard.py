"""DEV test-account publication and notification privacy guard."""
from datetime import date

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import department_roles
from apps.api.db import retry_write, transaction
from apps.api.settings import Settings
from apps.api.services import announcements
from apps.worker.main import deliver_notifications
from tests.outbox_support import dispatch_batch_after_undo
from tests.test_email_delivery import configuration, no_account_person, source
from tests.test_events import event as calendar_event
from tests.test_pwa_push import ready, subscribe


pytestmark = pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)


def enabled(h, value):
    object.__setattr__(h.settings, "publish_test_people_only", value)
    h.app.state.engine.update_execution_options(rota_write_settings=h.settings)


def rename(h, identity_id, name, person_id=None):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, identity_id).display_name = name
        if person_id:
            db.get(m.Person, person_id).display_name = name


def coordinator_approver(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.RoleBinding(identity_id=h.seed["identities"]["coordinator"],
            department_id=h.seed["department_id"], role="approver", starts_on=date(2020, 1, 1)))
        db.add(m.RoleBinding(identity_id=h.seed["identities"]["approver"],
            department_id=h.seed["department_id"], role="coordinator", starts_on=date(2020, 1, 1)))


def publish_as(h, draft, role="approver"):
    draft = h.lifecycle(draft, "submit", role="coordinator")["workspace"]
    draft = h.lifecycle(draft, "approve", role=role)["workspace"]
    return h.lifecycle(draft, "publish", role=role)


def privacy_draft(h, *, test_person):
    person = h.seed["people"][0]
    name = "Synthetic roster person (test)" if test_person else "Synthetic roster person"
    rename(h, h.seed["identities"]["coordinator"], "Synthetic coordinator")
    rename(h, h.seed["identities"]["approver"], "Synthetic publisher (test)")
    rename(h, h.seed["identities"]["staff"], name, person)
    coordinator_approver(h)
    draft = h.shift(h.workspace(name="Synthetic privacy rota"), people=[person])["workspace"]
    return draft


def test_test_actor_real_people_publish_is_refused_and_audited(h):
    enabled(h, True)
    draft = privacy_draft(h, test_person=False)
    refused = h.lifecycle(draft, "submit", role="coordinator")
    # Submission is allowed; the privacy boundary is the publish command.
    approved = h.lifecycle(refused["workspace"], "approve", role="approver")["workspace"]
    result = h.lifecycle(approved, "publish", role="approver", status=422)
    error = result["error"]
    assert error["code"] == "PUBLISH_TEST_PEOPLE_ONLY"
    assert error["message"] == "DEV test accounts publish only test people: 1 real-named people assigned"
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Publication.id).where(m.Publication.workspace_id == draft["id"])) is None
        assert not db.scalar(select(m.Outbox.id).where(m.Outbox.kind == "publication"))
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == "command.refused",
            m.AuditEvent.actor_id == h.seed["identities"]["approver"]).order_by(m.AuditEvent.created_at.desc()))
        assert audit and audit.detail["error_code"] == "PUBLISH_TEST_PEOPLE_ONLY"


def test_test_actor_all_test_rota_publishes_and_notifies_test_people(h):
    enabled(h, True)
    draft = privacy_draft(h, test_person=True)
    result = publish_as(h, draft)
    assert result["publication_id"]
    retry_write(h.app.state.engine, dispatch_batch_after_undo)
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == "publication:" + result["publication_id"]))
        assert event.state == "available_in_app"
        assert set(db.scalars(select(m.Notification.identity_id).where(m.Notification.outbox_id == event.id))) == {
            h.seed["identities"]["staff"]}


def test_non_test_owner_actor_with_real_people_is_unchanged(h):
    enabled(h, True)
    draft = privacy_draft(h, test_person=False)
    with transaction(h.app.state.engine, write=True) as db:
        owner = db.get(m.Identity, h.seed["identities"]["approver"])
        owner.display_name = "Synthetic owner"
    draft = h.lifecycle(draft, "submit", role="coordinator")["workspace"]
    draft = h.lifecycle(draft, "approve", role="approver")["workspace"]
    assert h.lifecycle(draft, "publish", role="approver")["publication_id"]


def test_setting_off_keeps_test_actor_publish_behaviour(h):
    enabled(h, False)
    draft = privacy_draft(h, test_person=False)
    assert publish_as(h, draft)["publication_id"]


@pytest.mark.parametrize("kind", ["availability", "request", "credential_impact"])
def test_test_actor_notifications_to_real_identity_are_suppressed_and_audited(h, kind):
    enabled(h, True)
    actor_id = h.seed["identities"]["coordinator"]
    person_id = h.seed["people"][0]
    rename(h, actor_id, "Synthetic actor (test)")
    rename(h, h.seed["identities"]["staff"], "Synthetic recipient", person_id)
    with transaction(h.app.state.engine, write=True) as db:
        event = m.Outbox(department_id=h.seed["department_id"], event_key=f"privacy:{kind}", kind=kind,
            payload={"actor_id": actor_id, "person_id": person_id, "count": 1,
                     "sessions": [{"publication_id": "missing", "person_id": person_id,
                                   "start": "2026-10-04T06:00:00+00:00", "end": "2026-10-04T10:00:00+00:00",
                                   "type": "Synthetic duty"}]})
        db.add(event)
        db.flush()
        roles = department_roles(db, h.seed["department_id"])
        identities = {row.id: row for row in db.scalars(select(m.Identity).where(
            m.Identity.id.in_(list(roles)), m.Identity.disabled.is_(False)))}
        identities.pop(actor_id)
        deliver_notifications(db, event, roles=roles, identities=identities)
        assert not db.scalar(select(m.Notification.id).where(m.Notification.outbox_id == event.id))
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.target_id == event.id))
        assert audit and audit.actor_id == actor_id and audit.detail == {"kind": kind, "count": 3}


def test_non_test_actor_notification_is_delivered(h):
    enabled(h, True)
    actor_id = h.seed["identities"]["coordinator"]
    rename(h, actor_id, "Synthetic actor")
    rename(h, h.seed["identities"]["staff"], "Synthetic recipient", h.seed["people"][0])
    with transaction(h.app.state.engine, write=True) as db:
        event = m.Outbox(department_id=h.seed["department_id"], event_key="privacy:owner", kind="availability",
            payload={"actor_id": actor_id, "person_id": h.seed["people"][0]})
        db.add(event)
        db.flush()
        roles = department_roles(db, h.seed["department_id"])
        identities = {row.id: row for row in db.scalars(select(m.Identity).where(
            m.Identity.id.in_(list(roles)), m.Identity.disabled.is_(False)))}
        deliver_notifications(db, event, roles=roles, identities=identities)
        assert db.scalar(select(m.Notification.id).where(m.Notification.outbox_id == event.id))


def test_setting_off_delivers_test_actor_notification(h):
    enabled(h, False)
    actor_id = h.seed["identities"]["coordinator"]
    rename(h, actor_id, "Synthetic actor (test)")
    rename(h, h.seed["identities"]["staff"], "Synthetic recipient", h.seed["people"][0])
    with transaction(h.app.state.engine, write=True) as db:
        event = m.Outbox(department_id=h.seed["department_id"], event_key="privacy:off", kind="availability",
            payload={"actor_id": actor_id, "person_id": h.seed["people"][0]})
        db.add(event)
        db.flush()
        roles = department_roles(db, h.seed["department_id"])
        identities = {row.id: row for row in db.scalars(select(m.Identity).where(
            m.Identity.id.in_(list(roles)), m.Identity.disabled.is_(False)))}
        deliver_notifications(db, event, roles=roles, identities=identities)
        assert db.scalar(select(m.Notification.id).where(m.Notification.outbox_id == event.id))


def test_old_test_publication_is_filtered_at_delivery(h):
    enabled(h, False)
    draft = privacy_draft(h, test_person=False)
    publication_id = publish_as(h, draft)["publication_id"]
    with transaction(h.app.state.engine, write=True) as db:
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == "publication:" + publication_id))
        event.payload = {key: value for key, value in event.payload.items() if key != "actor_id"}
    enabled(h, True)
    retry_write(h.app.state.engine, dispatch_batch_after_undo)
    with transaction(h.app.state.engine) as db:
        publication = db.get(m.Publication, publication_id)
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == "publication:" + publication_id))
        assert event.state == "available_in_app"
        notes = list(db.scalars(select(m.Notification).join(m.Outbox).where(
            m.Outbox.payload["publication_id"].as_string() == publication.id)))
        assert not notes
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.target_id == event.id))
        assert audit and audit.actor_id == publication.published_by
        assert set(audit.detail) == {"kind", "count"} and audit.detail["count"] > 0


def test_setting_defaults_off_and_reads_environment(h, monkeypatch):
    monkeypatch.delenv("FMUC_PUBLISH_TEST_PEOPLE_ONLY", raising=False)
    assert Settings.from_env().publish_test_people_only is False
    monkeypatch.setenv("FMUC_PUBLISH_TEST_PEOPLE_ONLY", "true")
    assert Settings.from_env().publish_test_people_only is True
    with pytest.raises(RuntimeError, match="FMUC_PUBLISH_TEST_PEOPLE_ONLY must be absent in production"):
        Settings(environment="test", deployment="production", publish_test_people_only=True).check(
            require_authentication=False)


def test_preview_matches_filtered_publication_delivery(h):
    enabled(h, True)
    draft = privacy_draft(h, test_person=True)
    draft = h.lifecycle(draft, "submit", role="coordinator")["workspace"]
    draft = h.lifecycle(draft, "approve", role="approver")["workspace"]
    preview = h.get("approver", f"/api/workspaces/{draft['id']}/publication-audience")
    assert {row["identity_id"] for row in preview["recipients"]} == {h.seed["identities"]["staff"]}
    assert preview["count"] == 1
    published = h.lifecycle(draft, "publish", role="approver")
    retry_write(h.app.state.engine, dispatch_batch_after_undo)
    with transaction(h.app.state.engine) as db:
        outbox = db.scalar(select(m.Outbox).where(m.Outbox.event_key == "publication:" + published["publication_id"]))
        assert set(db.scalars(select(m.Notification.identity_id).where(m.Notification.outbox_id == outbox.id))) == {
            row["identity_id"] for row in preview["recipients"]}


def test_non_publication_filters_in_app_email_and_push(ready):
    h = ready
    enabled(h, True)
    configuration(h)
    h.command("admin", "notification.policy.update", {
        "department_id": h.seed["department_id"], "push_enabled": True,
        "event_defaults": {"availability": {"email": "on", "push": "on"}},
        "reason": "Synthetic privacy channel coverage"}, revision=2)
    for role in ("staff", "approver"):
        subscribe(h, role, endpoint="https://fcm.googleapis.com/privacy-" + role, hint="privacy-" + role)
    rename(h, h.seed["identities"]["coordinator"], "Synthetic actor (test)")
    rename(h, h.seed["identities"]["staff"], "Synthetic recipient (test)", h.seed["people"][0])
    rename(h, h.seed["identities"]["approver"], "Synthetic non-test recipient")
    with transaction(h.app.state.engine, write=True) as db:
        event = m.Outbox(department_id=h.seed["department_id"], event_key="privacy:channels", kind="availability",
            payload={"actor_id": h.seed["identities"]["coordinator"], "person_id": h.seed["people"][0]})
        db.add(event)
        db.flush()
        dispatch_batch_after_undo(db)
        assert event.state == "available_in_app"
        for model, column in ((m.Notification, m.Notification.outbox_id),
                              (m.EmailDelivery, m.EmailDelivery.source_outbox_id),
                              (m.PushDelivery, m.PushDelivery.source_outbox_id)):
            assert set(db.scalars(select(model.identity_id).where(column == event.id))) == {
                h.seed["identities"]["staff"]}


@pytest.mark.parametrize("kind", ["announcement", "event"])
def test_bulk_and_email_only_audiences_are_filtered(h, kind):
    enabled(h, True)
    configuration(h)
    rename(h, h.seed["identities"]["coordinator"], "Synthetic actor (test)")
    rename(h, h.seed["identities"]["staff"], "Synthetic staff (test)", h.seed["people"][0])
    test_person = no_account_person(h, email_address="test-only@example.test")
    real_person = no_account_person(h, email_address="non-test@example.test")
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, test_person).display_name = "Synthetic email person (test)"
    audience = {"roles": ["staff"], "person_ids": [test_person, real_person]}
    if kind == "announcement":
        _, parent_id = source(h, audience=audience)
    else:
        record = calendar_event(h, audience=audience, also_email=True)
        with transaction(h.app.state.engine) as db:
            parent_id = db.scalar(select(m.Outbox.id).where(m.Outbox.kind == "event",
                m.Outbox.payload["event_id"].as_string() == record["id"]))
    retry_write(h.app.state.engine, dispatch_batch_after_undo)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Outbox, parent_id).state == "available_in_app"
        assert set(db.scalars(select(m.Notification.identity_id).where(m.Notification.outbox_id == parent_id))) == {
            h.seed["identities"]["staff"]}
        assert set(db.scalars(select(m.EmailDelivery.person_id).where(m.EmailDelivery.source_outbox_id == parent_id))) == {
            h.seed["people"][0], test_person}


def test_announcement_reconciliation_cannot_bypass_filter(h):
    enabled(h, False)
    rename(h, h.seed["identities"]["coordinator"], "Synthetic actor (test)")
    content_id, parent_id = source(h)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.DepartmentContent, content_id)
        row.sent_outbox_id = parent_id
        db.get(m.Outbox, parent_id).state = "available_in_app"
    enabled(h, True)
    with transaction(h.app.state.engine, write=True) as db:
        announcements.sync_receipts(db, db.get(m.DepartmentContent, content_id))
        assert not db.scalar(select(m.Notification.id).where(m.Notification.outbox_id == parent_id))
