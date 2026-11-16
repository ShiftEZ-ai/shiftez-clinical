"""Mixed commands see the same pending state as sequential single commands."""
import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk import REASON, preview
from .test_rule_catalogue_conflicts import line, most


def test_bulk_restored_catalogue_proposal_blocks_second_waiting_version(h):
    proposal = {"catalogue_id": "duties_per_week", "lines": [line(numbers=most(5))], "reason": REASON}
    original = h.command("coordinator", "rule.propose", proposal)["record"]
    declined = h.command("coordinator", "policy.decline", {"reason": REASON},
                         original["id"], original["revision"])["record"]
    with transaction(h.app.state.engine) as db:
        histories = db.scalar(select(func.count()).select_from(m.DefinitionHistory))
        policies = set(db.scalars(select(m.Policy.id)))
    payload = {"items": [
        {"item_kind": "policy.restore_proposal", "target_id": declined["id"],
         "expected_revision": declined["revision"], "payload": {}},
        {"item_kind": "rule.propose", "payload": proposal}], "reason": REASON}

    checked = preview(h, "coordinator", payload)
    assert checked["items"][0]["result"] in {"ready", "waiting"}
    assert checked["items"][1]["result"] == "problem"
    assert checked["items"][1]["issues"][0]["code"] == "PROPOSAL_WAITING"
    refused = h.command("coordinator", "bulk.apply", payload, status=409)["error"]
    assert refused["code"] == "PROPOSAL_WAITING" and refused["details"]["failed_index"] == 1
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Policy, original["id"])
        assert (row.status, row.revision) == ("retired", declined["revision"])
        assert set(db.scalars(select(m.Policy.id))) == policies
        assert db.scalar(select(func.count()).select_from(m.DefinitionHistory)) == histories
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == "bulk.apply")) is None


def test_bulk_add_tag_then_anonymise_removes_pending_membership(h):
    person = h.configure("people", {"display_name": "Synthetic former colleague", "active": False})["record"]
    tag = h.configure("tags", {"name": "Synthetic leaver group", "labels": ["people"]})["record"]
    payload = {"items": [
        {"item_kind": "tag.members", "target_id": tag["id"], "expected_revision": tag["revision"],
         "payload": {"add": [{"kind": "people", "id": person["id"]}]}},
        {"item_kind": "person.anonymise", "target_id": person["id"],
         "expected_revision": person["revision"], "payload": {}}], "reason": REASON}

    checked = preview(h, "admin", payload)
    assert checked["counts"]["problem"] == 0
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, person["id"]).display_name == person["display_name"]
        assert db.get(m.Tag, tag["id"]).revision == tag["revision"]
        assert db.scalar(select(m.TagMember.id).where(m.TagMember.member_id == person["id"])) is None
    applied = h.command("admin", "bulk.apply", payload)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Person, person["id"])
        assert row.tags == [] and row.display_name.startswith("Former staff member")
        assert db.scalar(select(m.TagMember.id).where(m.TagMember.member_id == person["id"])) is None
        histories = list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.batch_id == applied["batch_id"],
            m.DefinitionHistory.kind.in_(["tag.member_add", "tag.member_remove"]))))
        assert {history.kind for history in histories} == {"tag.member_add", "tag.member_remove"}
        assert len({history.object_id for history in histories}) == 1


@pytest.mark.parametrize("tagged", [False, True])
def test_bulk_people_creation_undo_uses_organisation_scope_and_guards_tags(h, tagged):
    tag = h.configure("tags", {"name": "Synthetic creation tag", "labels": ["people"]})["record"]
    payload = {"item_kind": "catalogue.create", "common": {"catalogue": "people"},
               "items": [{"payload": {"value": {"display_name": "Synthetic new colleague",
                                                  "tags": [tag["name"]] if tagged else []}}}], "reason": REASON}
    applied = h.command("admin", "bulk.apply", payload)
    person = applied["items"][0]["record"]
    checked = preview(h, "admin", applied["undo"]["payload"])
    assert checked["items"][0]["result"] == "ready"
    undone = h.command("admin", **applied["undo"])
    assert undone["items"][0]["results"][0]["removed"] == 1
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Person, person["id"])
        members = list(db.scalars(select(m.TagMember).where(m.TagMember.member_id == person["id"])))
        histories = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == person["id"])))
        assert row is None and not members
        assert {history.object_revision for history in histories} == {1, 2}


@pytest.mark.parametrize("change", ["add", "remove"])
def test_bulk_creation_undo_observes_pending_tag_add_and_remove(h, change):
    service = h.configure("services", {"name": "Synthetic clinic creation", "starts_on": "2020-01-01"})["record"]
    tag = h.configure("tags", {"name": "Synthetic clinic tag", "labels": ["services"]})["record"]
    ref = {"kind": "services", "id": service["id"]}
    if change == "remove":
        tag = h.command("admin", "tag.members", {"add": [ref], "reason": REASON}, tag["id"], tag["revision"])["record"]
    payload = {"items": [
        {"item_kind": "tag.members", "target_id": tag["id"], "expected_revision": tag["revision"],
         "payload": {change: [ref]}},
        {"item_kind": "catalogue.undo_create", "target_id": service["id"],
         "expected_revision": service["revision"], "payload": {"catalogue": "services"}}], "reason": REASON}
    checked = preview(h, "admin", payload)
    if change == "add":
        assert checked["items"][1]["issues"][0]["code"] == "INTEGRITY_CONFLICT"
        refused = h.command("admin", "bulk.apply", payload, status=409)["error"]
        assert refused["code"] == "INTEGRITY_CONFLICT" and refused["details"]["failed_index"] == 1
    else:
        assert checked["counts"]["problem"] == 0
        h.command("admin", "bulk.apply", payload)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Service, service["id"])
        assert (row is None) == (change == "remove")
        if row is not None:
            assert row.revision == service["revision"]
        assert db.scalar(select(m.TagMember.id).where(m.TagMember.member_id == service["id"])) is None
        assert db.get(m.Tag, tag["id"]).revision == tag["revision"] + int(change == "remove")
