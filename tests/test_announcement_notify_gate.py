"""Saving an announcement never turns an email preference into permission to send."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.worker import email_delivery
from tests.outbox_support import process_one_after_undo as process_one
from .test_bulk import preview
from .test_email_api import settings, staff_address


def announcement():
    return {"name": "Private announcement draft", "description": "Saved without sending",
            "starts_on": "2020-01-01", "audience": {"roles": ["staff"]}, "notify": False}


def prepare(h, source, form):
    staff_address(h)
    row = None
    if form not in {"create", "bulk_create"}:
        row = h.configure("department_content", announcement(), role="coordinator")["record"]
        if source == "saved":
            with transaction(h.app.state.engine, write=True) as db:
                db.get(m.DepartmentContent, row["id"]).also_email = True
    settings(h, smtp_enabled=True, smtp_host="127.0.0.1", smtp_starttls=False,
             sender_address="notices@example.test", announcement_default=source == "default")
    return row


def save(h, source, form, row, *, notify=False):
    value = {**announcement(), "description": "A checked edit", "notify": notify}
    if source == "explicit":
        value["also_email"] = True
    if form in {"create", "update"}:
        return h.configure("department_content", value, row["id"] if row else None,
                           row["revision"] if row else None, role="coordinator")["record"]
    if form == "bulk_create":
        item = {"item_kind": "catalogue.create", "payload": {"catalogue": "department_content", "value": value}}
    else:
        item = {"item_kind": "catalogue.bulk_update", "payload": {"catalogue": "department_content",
                "items": [{"id": row["id"], "revision": row["revision"]}],
                "set": {"description": value["description"], "notify": notify}}}
        if form == "nested_bulk_update":
            item = {"changes": [item]}
    body = {"items": [item], "reason": "Save without notifying"}
    assert preview(h, "coordinator", body)["counts"]["ready"] == 1
    h.command("coordinator", "bulk.apply", body)


def email_spy(monkeypatch):
    sent = []
    monkeypatch.setattr(email_delivery, "send", lambda db, delivery, *args: sent.append(delivery.address))
    return sent


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("form,source", [
    ("create", "explicit"), ("create", "default"),
    ("update", "explicit"), ("update", "saved"), ("update", "default"),
    ("bulk_create", "explicit"), ("bulk_create", "default"),
    ("bulk_update", "saved"), ("bulk_update", "default"),
    ("nested_bulk_update", "saved"), ("nested_bulk_update", "default"),
])
def test_notify_false_never_queues_or_delivers_email(request, database, form, source, monkeypatch):
    h = request.getfixturevalue(database)
    row = prepare(h, source, form)
    sent = email_spy(monkeypatch)
    save(h, source, form, row)
    process_one(h.app.state.engine, settings=h.settings)
    assert sent == []
    with transaction(h.app.state.engine) as db:
        assert not list(db.scalars(select(m.Outbox).where(m.Outbox.kind.in_(["announcement", "email"]))))
        assert not list(db.scalars(select(m.EmailDelivery)))
    assert not h.get("staff", "/api/notifications")["notifications"]


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("source,form", [("explicit", "create"), ("default", "create"), ("saved", "update")])
def test_notify_true_delivers_the_effective_email_choice(request, database, source, form, monkeypatch):
    h = request.getfixturevalue(database)
    row = prepare(h, source, form)
    sent = email_spy(monkeypatch)
    save(h, source, form, row, notify=True)
    process_one(h.app.state.engine, settings=h.settings)
    assert sent == ["work@example.test"]
    with transaction(h.app.state.engine) as db:
        assert [delivery.state for delivery in db.scalars(select(m.EmailDelivery))] == ["sent"]
    assert len(h.get("staff", "/api/notifications")["notifications"]) == 1


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_explicit_reminder_can_deliver_a_home_only_announcement(request, database, monkeypatch):
    h = request.getfixturevalue(database)
    prepare(h, "explicit", "create")
    row = h.configure("department_content", {**announcement(), "also_email": True}, role="coordinator")["record"]
    sent = email_spy(monkeypatch)
    h.command("coordinator", "department_content.remind", {"reason": "Deliberately notify the audience"},
              row["id"], row["revision"])
    process_one(h.app.state.engine, settings=h.settings)
    assert sent == ["work@example.test"]
