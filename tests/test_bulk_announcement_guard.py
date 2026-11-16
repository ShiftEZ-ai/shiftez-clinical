"""Every bulk catalogue envelope shares the individual announcement-send guard."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from .test_bulk import REASON, preview
from .test_bulk_dev_catalogue_undo import content


def state(h):
    with transaction(h.app.state.engine) as db:
        return {cls.__tablename__: [record(row) for row in db.scalars(select(cls).order_by(cls.id))]
                for cls in (m.DepartmentContent, m.AnnouncementReceipt, m.Outbox, m.Notification,
                            m.DefinitionHistory, m.Person, m.Organisation)}


def item(form, row, notify):
    value = {**content(), "description": "Changed without bypassing the send guard", "notify": notify}
    if form == "create":
        return {"item_kind": "catalogue.create", "payload": {"catalogue": "department_content", "value": value}}
    if form in {"update", "fields"}:
        result = {"item_kind": "catalogue.update", "target_id": row["id"], "expected_revision": row["revision"],
                  "payload": {"catalogue": "department_content"}}
        if form == "fields":
            result["changes"] = [{"field": key, "how": "set", "value": value[key]} for key in ("description", "notify")]
        else:
            result["payload"]["value"] = value
        return result
    action = {"item_kind": "catalogue.bulk_update", "payload": {"catalogue": "department_content",
        "items": [{"id": row["id"], "revision": row["revision"]}],
        "set": {"description": value["description"], "notify": notify}}}
    return {"changes": [action]} if form == "nested_bulk_update" else action


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("form", ["create", "update", "fields", "bulk_update", "nested_bulk_update"])
@pytest.mark.parametrize("notify", [pytest.param(True, id="send"), pytest.param(False, id="save")])
def test_announcement_send_guard_covers_every_bulk_write_envelope(request, database, form, notify):
    h = request.getfixturevalue(database)
    row = h.configure("department_content", content(), role="coordinator")["record"]
    with transaction(h.app.state.engine) as db:
        person = record(db.get(m.Person, h.seed["people"][0]))
    safe = {"item_kind": "catalogue.update", "target_id": person["id"], "expected_revision": person["revision"],
        "payload": {"catalogue": "people"}, "changes": [{"field": "title", "value": "Atomic announcement test"}]}
    body = {"items": [safe, item(form, row, notify)], "reason": REASON}
    before = state(h)
    checked = preview(h, "coordinator", body)
    assert state(h) == before
    if notify:
        assert checked["items"][1]["result"] == "problem"
        issue = checked["items"][1]["issues"][0]
        assert issue["code"] == "BULK_EXTERNAL_EFFECT"
        assert issue["message"] == "Announcements that notify people must be sent individually"
        refused = h.command("coordinator", "bulk.apply", body, status=409)["error"]
        assert refused["code"] == issue["code"] and refused["message"] == issue["message"]
        assert refused["details"]["failed_index"] == 1
        assert state(h) == before  # The earlier write, receipts and delivery all roll back.
    else:
        assert checked["counts"]["ready"] == 2
        applied = h.command("coordinator", "bulk.apply", body)
        assert applied["counts"]["changed"] == 2
        after = state(h)
        assert after["outbox"] == before["outbox"] and after["notifications"] == before["notifications"]
        changed = next(part for part in after["department_content"] if part["description"] ==
                       "Changed without bypassing the send guard")
        assert form == "create" or changed["id"] == row["id"] and changed["revision"] == row["revision"] + 1


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_individual_announcement_create_and_update_can_still_notify(request, database):
    h = request.getfixturevalue(database)
    created = h.configure("department_content", {**content(), "notify": True}, role="coordinator")["record"]
    updated = h.configure("department_content", {**content(), "notify": True, "description": "Individual update"},
                          created["id"], created["revision"], role="coordinator")["record"]
    with transaction(h.app.state.engine) as db:
        sends = list(db.scalars(select(m.Outbox).where(m.Outbox.kind == "announcement")))
        assert len(sends) == 2
        assert {part.payload["revision"] for part in sends} == {created["revision"], updated["revision"]}
        assert all(part.payload["content_id"] == created["id"] and part.payload["notify_inbox"] for part in sends)
