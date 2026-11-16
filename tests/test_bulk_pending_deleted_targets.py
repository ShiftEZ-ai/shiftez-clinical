"""Later batch commands cannot attach records to an object already removed by Undo."""
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import catalogue, events, leave_types, leavers, policies
from packages.contracts import schemas as s
from .test_bulk import REASON, preview
from .test_events import event_type, value as event_value


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def deleted_h(request):
    return request.getfixturevalue(request.param)


@pytest.mark.parametrize("catalogue,operation", [("people", "tag"), ("services", "tag"), ("people", "wish")])
def test_bulk_creation_undo_rejects_later_reference_and_rolls_back(deleted_h, catalogue, operation):
    h = deleted_h
    value = {"display_name": "Synthetic removed colleague"} if catalogue == "people" else {
        "name": "Synthetic removed clinic", "starts_on": "2020-01-01"}
    row = h.configure(catalogue, value)["record"]
    tag = h.configure("tags", {"name": "Later attachment", "labels": [catalogue]})["record"]
    later = ({"item_kind": "tag.members", "target_id": tag["id"], "expected_revision": tag["revision"],
              "payload": {"add": [{"kind": catalogue, "id": row["id"]}]}} if operation == "tag" else {
        "item_kind": "wish.create", "payload": {"person_id": row["id"], "kind": "prefer_off",
            "first_day": "2026-10-05", "last_day": "2026-10-05", "note": "Synthetic private note"}})
    payload = {"reason": REASON, "items": [
        {"item_kind": "catalogue.undo_create", "target_id": row["id"], "expected_revision": row["revision"],
         "payload": {"catalogue": catalogue}}, later]}
    with transaction(h.app.state.engine) as db:
        histories = db.scalar(select(func.count()).select_from(m.DefinitionHistory))
    checked = preview(h, "admin", payload)
    assert checked["items"][1]["result"] == "problem"
    assert checked["items"][1]["issues"][0]["code"] == "NOT_FOUND"
    error = h.command("admin", "bulk.apply", payload, status=409)["error"]
    assert error["code"] == "NOT_FOUND" and error["details"]["failed_index"] == 1
    with transaction(h.app.state.engine) as db:
        cls = m.Person if catalogue == "people" else m.Service
        assert db.get(cls, row["id"]).revision == row["revision"]
        assert db.get(m.Tag, tag["id"]).revision == tag["revision"]
        assert db.scalar(select(m.TagMember.id).where(m.TagMember.member_id == row["id"])) is None
        assert db.scalar(select(m.StaffWish.id).where(m.StaffWish.person_id == row["id"])) is None
        assert db.scalar(select(func.count()).select_from(m.DefinitionHistory)) == histories
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == "bulk.apply")) is None


@pytest.mark.parametrize("reference", ["policy_tag", "event_person", "event_service", "event_audience_tag",
    "event_tag", "event_organiser", "event_presenter", "rotation_template_service", "rotation_template_type", "leave_text"])
def test_reference_validators_reject_pending_deleted_objects(deleted_h, reference):
    h = deleted_h
    if reference in {"event_person", "event_organiser", "event_presenter"}:
        kind, value = "people", {"display_name": "Synthetic audience person"}
    elif reference in {"event_service", "rotation_template_service"}:
        kind, value = "services", {"name": "Synthetic referenced clinic", "starts_on": "2020-01-01"}
    elif reference == "rotation_template_type":
        kind, value = "rotation_types", {"name": "Synthetic rotation type", "starts_on": "2020-01-01"}
    elif reference == "leave_text":
        kind, value = "leave_types", {"name": "Synthetic leave", "short_code": "SYNTH"}
    else:
        kind, value = "tags", {"name": "Synthetic reference", "labels": ["events" if reference == "event_tag" else "services"]}
    created = h.configure(kind, value)["record"]
    typ = event_type(h) if reference in {"event_organiser", "event_presenter"} else None
    actor = Actor(h.seed["identities"]["admin"], h.seed["department_id"], h.seed["organisation_id"],
                  "Synthetic admin", frozenset({"admin"}), None, "synthetic-session")
    with Session(h.app.state.engine, autoflush=False) as db:
        row = db.get(catalogue.CATALOGUES[kind], created["id"])
        if reference == "policy_tag":
            call = lambda: policies.condition_references(db, actor, s.Conditions(conditions=[
                {"field": "service_ids", "values": [row.id]}]))
        elif reference == "event_tag":
            call = lambda: events.check_tags(db, actor, [row.id])
        elif typ:
            event = s.EventInput.model_validate(event_value(typ, **{
                "organiser_ids" if reference == "event_organiser" else "presenter_ids": [row.id]}))
            call = lambda: events.prepare(db, actor, event)
        elif reference.startswith("event_"):
            audience = (s.Audience(where={"conditions": [{"field": "service_ids", "values": [row.id]}]})
                if reference == "event_audience_tag" else s.Audience(**{
                    "person_ids" if kind == "people" else "service_ids": [row.id]}))
            call = lambda: events.check_audience(db, actor, audience)
        elif reference.startswith("rotation_template_"):
            template = s.RotationTemplateInput(name="Synthetic plan", blocks=[{
                "service_id" if kind == "services" else "rotation_type_id": row.id}])
            call = lambda: catalogue.validate_references(db, actor, "rotation_templates", template)
        else:
            call = lambda: leave_types.resolve(db, actor, text=row.name)
        call()
        db.delete(row)
        with pytest.raises(Problem) as refused:
            call()
        assert refused.value.code in {"NOT_FOUND", "AUDIENCE_REFERENCE", "EVENT_TAG", "EVENT_PERSON"}


def test_anonymise_rejects_a_pending_deleted_person(deleted_h):
    h = deleted_h
    person = h.configure("people", {"display_name": "Synthetic removed leaver", "active": False})["record"]
    actor = Actor(h.seed["identities"]["admin"], h.seed["department_id"], h.seed["organisation_id"],
                  "Synthetic admin", frozenset({"admin"}), None, "synthetic-session")
    command = s.Command(kind="person.anonymise", target_id=person["id"], expected_revision=person["revision"],
                        payload={"reason": REASON})
    with Session(h.app.state.engine, autoflush=False) as db:
        row = db.get(m.Person, person["id"])
        db.delete(row)
        with pytest.raises(Problem) as refused:
            leavers.anonymise(db, actor, command, h.settings)
        assert refused.value.code == "NOT_FOUND"
        assert row.display_name == person["display_name"] and row.revision == person["revision"]


def test_bulk_rank_creation_ignores_a_membership_removed_earlier_in_the_batch(deleted_h):
    h = deleted_h
    person = h.configure("people", {"display_name": "Synthetic replacement rank owner"})["record"]
    value = {"person_id": person["id"], "category_id": h.seed["category"], "starts_on": "2027-01-01"}
    old = h.command("coordinator", "membership.create", value)["record"]
    body = {"reason": REASON, "items": [
        {"item_kind": "membership.undo_create", "payload": {"items": [{"id": old["id"], "revision": old["revision"]}]}},
        {"item_kind": "membership.bulk_create", "payload": {"person_ids": [person["id"]],
            "category_id": h.seed["category"], "starts_on": "2027-01-01"}}]}
    checked = preview(h, "coordinator", body)
    assert checked["counts"]["problem"] == 0, checked
    result = h.command("coordinator", "bulk.apply", body)
    assert result["items"][1]["results"][0]["added"] == 1
    with transaction(h.app.state.engine) as db:
        ranks = list(db.scalars(select(m.Membership).where(m.Membership.person_id == person["id"])))
        assert len(ranks) == 1 and ranks[0].id != old["id"]
