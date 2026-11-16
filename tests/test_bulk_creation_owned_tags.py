"""Person creation Undo removes its own tag side effects, never independent references."""
from datetime import date

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import catalogue, tags
from apps.api.services.bulk import _batch
from packages.contracts import schemas as s
from .test_bulk import REASON, preview
from .test_bulk_single_commands import undo as undo_command


def create_person(h, bulk, *, name="Synthetic tagged colleague", tag="Creation-owned team"):
    value = {"display_name": name, "tags": [tag]}
    if bulk:
        reply = h.command("coordinator", "bulk.apply", {"item_kind": "catalogue.create", "common": {"catalogue": "people"},
            "items": [{"payload": {"value": value}}], "reason": REASON})
        return reply["items"][0]["record"], reply["undo"]
    row = h.configure("people", value, role="coordinator")["record"]
    return row, {"kind": "catalogue.undo_create", "target_id": row["id"], "expected_revision": row["revision"],
                 "payload": {"catalogue": "people", "reason": REASON}}


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("existing_tag", [False, True])
def test_tagged_person_creation_undo_removes_only_creation_memberships(h, bulk, existing_tag):
    if existing_tag:
        tag = h.configure("tags", {"name": "Creation-owned team", "labels": ["people"]})["record"]
    person, undo = create_person(h, bulk)
    with transaction(h.app.state.engine) as db:
        member = db.scalar(select(m.TagMember).where(m.TagMember.member_id == person["id"]))
        member_id, tag_id = member.id, member.tag_id
        version = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == member.id))
        assert version.snapshot["created_with_person_id"] == person["id"]
        assert version.snapshot["revision"] == 1
        if existing_tag:
            assert tag_id == tag["id"]
    if bulk:
        assert preview(h, "coordinator", undo["payload"])["counts"]["problem"] == 0
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Person, person["id"]) is not None
            assert db.get(m.TagMember, member_id) is not None
    undo_command(h, "coordinator", undo)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, person["id"]) is None
        assert db.get(m.TagMember, member_id) is None
        assert db.get(m.Tag, tag_id) is not None
        versions = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == member_id)))
        assert {(version.kind, version.object_revision) for version in versions} == {
            ("tag.member_add", 1), ("tag.member_remove", 2)}
        assert all(version.actor_id == h.seed["identities"]["coordinator"] for version in versions)
        if bulk:
            removed = next(version for version in versions if version.kind == "tag.member_remove")
            assert removed.batch_id is not None


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("reference", ["relationship", "independent_tag"])
def test_person_creation_undo_preserves_owned_tags_when_other_references_refuse(h, bulk, reference):
    person, undo = create_person(h, bulk)
    with transaction(h.app.state.engine, write=True) as db:
        owned = db.scalar(select(m.TagMember).where(m.TagMember.member_id == person["id"]))
        owned_id = owned.id
        if reference == "relationship":
            db.add(m.Membership(department_id=h.seed["department_id"], person_id=person["id"],
                category_id=h.seed["category"], starts_on=date(2026, 1, 1)))
        else:
            tag = m.Tag(department_id=h.seed["department_id"], name="Independent team", labels=["people"])
            db.add(tag); db.flush()
            # A different source can create references without rewriting Person's catalogue revision.
            db.add(m.TagMember(department_id=h.seed["department_id"], tag_id=tag.id, kind="people", member_id=person["id"]))
    if bulk:
        assert preview(h, "coordinator", undo["payload"])["items"][0]["issues"][0]["code"] == "INTEGRITY_CONFLICT"
    refused = undo_command(h, "coordinator", undo, status=409)["error"]
    assert refused["code"] == "INTEGRITY_CONFLICT"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, person["id"]).revision == 1
        assert db.get(m.TagMember, owned_id).revision == 1
        assert db.scalar(select(m.DefinitionHistory.id).where(m.DefinitionHistory.object_id == owned_id,
            m.DefinitionHistory.kind == "tag.member_remove")) is None


@pytest.mark.parametrize("command", ["tag.members", "catalogue.update"])
def test_later_tag_additions_never_claim_creation_provenance(h, command):
    person, undo = create_person(h, True)
    tag = h.configure("tags", {"name": "Later independent team", "labels": ["people"]})["record"]
    if command == "tag.members":
        h.command("coordinator", command, {"add": [{"kind": "people", "id": person["id"]}], "reason": REASON},
                  tag["id"], tag["revision"])
    else:
        h.configure("people", {"display_name": person["display_name"], "tags": [*person["tags"], tag["name"]]},
                    person["id"], person["revision"], role="coordinator")
    with transaction(h.app.state.engine) as db:
        member = db.scalar(select(m.TagMember).where(m.TagMember.tag_id == tag["id"]))
        version = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == member.id))
        assert "created_with_person_id" not in version.snapshot
    assert h.command("coordinator", **undo, status=409)["error"]["code"] == "STALE_REVISION"


def test_person_creation_undo_checks_pending_independent_memberships_before_removing_owned_ones(h):
    person, undo = create_person(h, True)
    tag = h.configure("tags", {"name": "Pending independent team", "labels": ["people"]})["record"]
    actor = Actor(h.seed["identities"]["coordinator"], h.seed["department_id"], h.seed["organisation_id"],
                  "Synthetic coordinator", frozenset({"coordinator"}), None, "synthetic-session")
    command = s.Command(kind="catalogue.undo_create", target_id=person["id"], expected_revision=person["revision"],
                        payload={"catalogue": "people", "reason": REASON})
    with transaction(h.app.state.engine, write=True) as db:
        with _batch(db, actor, m.uid()):
            owned = db.scalar(select(m.TagMember).where(m.TagMember.member_id == person["id"]))
            independent = tags.add_member(db, actor, db.get(m.Tag, tag["id"]), "people", person["id"], REASON)
            with pytest.raises(Problem) as refused:
                catalogue.undo_catalogue_created(db, actor, command)
            assert refused.value.code == "INTEGRITY_CONFLICT"
            assert owned not in db.deleted and owned.revision == 1
            tags.remove_member(db, actor, independent, REASON)
            assert catalogue.undo_catalogue_created(db, actor, command)["removed"] == 1
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, person["id"]) is None
        assert db.scalar(select(m.TagMember.id).where(m.TagMember.member_id == person["id"])) is None


def test_bulk_tagged_people_undo_batches_provenance_and_reference_queries(h):
    created = h.command("coordinator", "bulk.apply", {"item_kind": "catalogue.create", "common": {"catalogue": "people"},
        "items": [{"payload": {"value": {"display_name": f"Synthetic tagged colleague {number}", "tags": ["Creation-owned team"]}}}
                  for number in range(40)], "reason": REASON})
    queries = []
    def queried(*args):
        if args[2].lstrip().upper().startswith("SELECT"):
            queries.append(args[2])
    event.listen(h.app.state.engine, "before_cursor_execute", queried)
    try:
        undone = h.command("coordinator", **created["undo"])
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", queried)
    assert undone["count"] == 40
    assert sum("FROM definition_history" in query for query in queries) <= 4
    assert sum("FROM tag_members" in query for query in queries) <= 4
    assert len(queries) < 90
    with transaction(h.app.state.engine) as db:
        ids = [item["record"]["id"] for item in created["items"]]
        assert db.scalar(select(m.Person.id).where(m.Person.id.in_(ids))) is None
        assert db.scalar(select(m.TagMember.id).where(m.TagMember.member_id.in_(ids))) is None
