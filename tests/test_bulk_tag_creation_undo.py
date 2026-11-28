"""Tag creation Undo compensates exactly its own initial memberships."""
import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.services import tags
from .governance_support import retire_rule
from .test_bulk import preview
from .test_bulk_single_commands import undo as undo_command
from .test_rule_catalogue_api import approve, line, propose


REASON = "Synthetic tag creation Undo"


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def tag_h(request):
    return request.getfixturevalue(request.param)


def create_tag(h, bulk):
    payload = {"catalogue": "tags", "value": {"name": "Synthetic creation team", "labels": ["people", "services"]},
        "members": {"add": [{"kind": "people", "id": h.seed["people"][0]},
                             {"kind": "services", "id": h.seed["services"][0]}]}, "reason": REASON}
    if bulk:
        result = h.command("coordinator", "bulk.apply", {"item_kind": "catalogue.create",
            "items": [{"payload": payload}], "reason": REASON})
        return result["items"][0]["record"], result["undo"]
    row = h.command("coordinator", "catalogue.create", payload)["record"]
    return row, {"kind": "catalogue.undo_create", "target_id": row["id"], "expected_revision": row["revision"],
                 "payload": {"catalogue": "tags", "reason": REASON}}


@pytest.mark.parametrize("bulk", [False, True])
def test_tag_creation_undo_removes_only_initial_members_and_syncs_people(tag_h, bulk):
    h = tag_h
    row, undo = create_tag(h, bulk)
    with transaction(h.app.state.engine) as db:
        members = list(db.scalars(select(m.TagMember).where(m.TagMember.tag_id == row["id"])))
        ids = [member.id for member in members]
        assert row["name"] in db.get(m.Person, h.seed["people"][0]).tags
        for member in members:
            version = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == member.id))
            assert version.snapshot["created_with_tag_id"] == row["id"]
    if bulk:
        check = preview(h, "coordinator", undo["payload"])
        assert check["counts"]["problem"] == 0, check["items"][0]["issues"]
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Tag, row["id"]) is not None
            assert row["name"] in db.get(m.Person, h.seed["people"][0]).tags
    deletions = []
    def deleting(*args):
        statement = args[2].lstrip().upper()
        if statement.startswith(("DELETE FROM TAGS ", "DELETE FROM TAG_MEMBERS ")):
            deletions.append(statement)
    event.listen(h.app.state.engine, "before_cursor_execute", deleting)
    try:
        undo_command(h, "coordinator", undo)
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", deleting)
    assert deletions and "DELETE FROM TAG_MEMBERS " in deletions[0]
    assert "DELETE FROM TAGS " in deletions[-1]
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Tag, row["id"]) is None
        assert not db.scalar(select(m.TagMember.id).where(m.TagMember.tag_id == row["id"]))
        assert row["name"] not in db.get(m.Person, h.seed["people"][0]).tags
        assert len(list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id.in_(ids),
            m.DefinitionHistory.kind == "tag.member_remove")))) == 2


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("independent", ["new", "readded"])
def test_tag_creation_undo_preserves_independent_members_even_without_a_tag_revision_change(tag_h, bulk, independent):
    h = tag_h
    row, undo = create_tag(h, bulk)
    actor = Actor(h.seed["identities"]["coordinator"], h.seed["department_id"], h.seed["organisation_id"],
                  "Synthetic coordinator", frozenset({"coordinator"}), None, "synthetic-session")
    with transaction(h.app.state.engine, write=True) as db:
        tag = db.get(m.Tag, row["id"])
        target = h.seed["services"][0 if independent == "readded" else 1]
        if independent == "readded":
            member = db.scalar(select(m.TagMember).where(m.TagMember.tag_id == row["id"],
                m.TagMember.kind == "services", m.TagMember.member_id == target))
            tags.remove_member(db, actor, member, REASON)
        replacement = tags.add_member(db, actor, tag, "services", target, REASON)
        replacement_id = replacement.id
        version = next(version for version in db.new if isinstance(version, m.DefinitionHistory)
                       and version.object_id == replacement_id)
        assert "created_with_tag_id" not in version.snapshot
    if bulk:
        assert preview(h, "coordinator", undo["payload"])["items"][0]["issues"][0]["code"] == "INTEGRITY_CONFLICT"
    assert undo_command(h, "coordinator", undo, status=409)["error"]["code"] == "INTEGRITY_CONFLICT"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Tag, row["id"]).revision == row["revision"]
        assert db.get(m.TagMember, replacement_id) is not None
        assert row["name"] in db.get(m.Person, h.seed["people"][0]).tags


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("style", ["legacy", "catalogue", "workspace"])
@pytest.mark.parametrize("status", ["proposed", "active", "retired"])
def test_tag_creation_undo_keeps_initial_members_when_independent_rule_names_tag(h, bulk, style, status):
    row, undo = create_tag(h, bulk)
    scope = {"match": "all", "conditions": [{"field": "person_ids", "op": "any", "values": [row["id"]]}]}
    if style == "catalogue":
        policy = propose(h, "duties_per_week", [line(who=scope, numbers={"max": {"value": 3}})])["record"]
    else:
        policy = h.command("coordinator", "policy.create", {"name": "Synthetic tag-dependent rule", "owner": "Test",
            "source": REASON, "starts_on": "2026-01-01", "definition": {"rule": {"kind": "min_rest", "hours": 11},
            "scope": {"conditions": scope}, "severity": "mandatory",
            "workspace_id": h.workspace()["id"] if style == "workspace" else None}})["record"]
    if status in {"active", "retired"}:
        policy = approve(h, policy)
    if status == "retired":
        policy = retire_rule(h, policy, REASON)["record"]
    if bulk:
        check = preview(h, "coordinator", undo["payload"])
        assert check["items"][0]["issues"][0]["code"] == "INTEGRITY_CONFLICT"
    assert undo_command(h, "coordinator", undo, status=409)["error"]["code"] == "INTEGRITY_CONFLICT"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Tag, row["id"]).revision == 1
        assert db.scalar(select(m.TagMember.id).where(m.TagMember.tag_id == row["id"])) is not None
        assert row["name"] in db.get(m.Person, h.seed["people"][0]).tags
        assert db.get(m.Policy, policy["id"]).definition == policy["definition"]


def test_bulk_tag_creation_undo_batches_policy_provenance_and_reference_queries(h):
    result = h.command("coordinator", "bulk.apply", {"item_kind": "catalogue.create",
        "items": [{"payload": {"catalogue": "tags", "value": {"name": f"Synthetic owned tag {number}", "labels": ["services"]},
            "members": {"add": [{"kind": "services", "id": h.seed["services"][0]}]}}} for number in range(40)],
        "reason": REASON})
    queries = []
    def queried(*args):
        if args[2].lstrip().upper().startswith("SELECT"):
            queries.append(args[2])
    event.listen(h.app.state.engine, "before_cursor_execute", queried)
    try:
        undone = h.command("coordinator", **result["undo"])
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", queried)
    assert undone["counts"]["changed"] == 40
    assert sum("FROM policies" in query for query in queries) <= 2
    assert sum("FROM definition_history" in query for query in queries) <= 4
    assert sum("FROM tag_members" in query for query in queries) <= 4
    assert len(queries) < 90
    with transaction(h.app.state.engine) as db:
        ids = [item["record"]["id"] for item in result["items"]]
        assert db.scalar(select(m.Tag.id).where(m.Tag.id.in_(ids))) is None
        assert db.scalar(select(m.TagMember.id).where(m.TagMember.tag_id.in_(ids))) is None


def test_bulk_tag_creation_undo_sees_rule_created_after_reference_cache_was_loaded(h):
    first = h.configure("tags", {"name": "Synthetic first team", "labels": ["people"]}, role="coordinator")["record"]
    second, undo = create_tag(h, False)
    with transaction(h.app.state.engine) as db:
        policies = set(db.scalars(select(m.Policy.id)))
    result = h.command("coordinator", "bulk.apply", {"reason": REASON, "items": [
        {"item_kind": "catalogue.undo_create", "target_id": first["id"], "expected_revision": first["revision"],
         "payload": {"catalogue": "tags"}},
        {"item_kind": "rule.propose", "payload": {"catalogue_id": "duties_per_week", "lines": [line(
            who={"match": "all", "conditions": [{"field": "tags", "values": [second["name"]]}]},
            numbers={"max": {"value": 3}})]}},
        {"item_kind": undo["kind"], "target_id": undo["target_id"], "expected_revision": undo["expected_revision"],
         "payload": undo["payload"]}]}, status=409)
    assert result["error"]["code"] == "INTEGRITY_CONFLICT"
    assert result["error"]["details"]["failed_index"] == 2
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Tag, first["id"]) is not None
        assert db.get(m.Tag, second["id"]) is not None
        assert second["name"] in db.get(m.Person, h.seed["people"][0]).tags
        assert set(db.scalars(select(m.Policy.id))) == policies


def test_rule_tag_references_include_embedded_selectors_and_tolerate_unstructured_settings():
    tag = m.Tag(id="synthetic-tag-id", name="Synthetic team")
    definition = {"rule": {"settings": {"tags": 2}, "partners": {"match": "all", "conditions": [
        {"field": "tags", "op": "any", "values": [tag.id]}]}}}
    assert tags.lines_naming(definition, tag) == 1
