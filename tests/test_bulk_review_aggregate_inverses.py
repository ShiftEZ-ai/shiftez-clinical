"""Aggregate decisions and transitive rank moves keep complete, admissible inverses."""
import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk import REASON, preview
from .test_bulk_nested_limits import limits
from .test_request_decide_many import decide, pattern_row, request_row


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_bulk_request_batch_undo_returns_per_record_inverse(request, database, decision):
    h = request.getfixturevalue(database)
    sources = [request_row(h), pattern_row(h)]
    decided = decide(h, sources, decision=decision, role="admin")
    body = {"items": [{"item_kind": "request.undo", "payload": {"batch_id": decided["batch_id"]}}], "reason": REASON}
    checked = preview(h, "admin", body)
    assert checked["counts"]["problem"] == 0, [(issue["code"], issue["message"])
        for row in checked["items"] for issue in row["issues"]]
    applied = h.command("admin", "bulk.apply", body)
    assert len(applied["undo"]["payload"]["items"]) == 2
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RequestRecord, sources[0]["id"]).status == "submitted"
        assert db.get(m.AvailabilityPattern, sources[1]["id"]).status == "proposed"
    h.command("admin", **applied["undo"])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RequestRecord, sources[0]["id"]).status == ("approved" if decision == "approve" else "rejected")
        assert db.get(m.AvailabilityPattern, sources[1]["id"]).status == ("active" if decision == "approve" else "declined")


@pytest.mark.parametrize("maximum", [1, 4, 5])
def test_bulk_tag_members_counts_every_member_and_undo_uses_the_same_limit(h, maximum):
    tag = h.configure("tags", {"name": "Synthetic counted team", "labels": ["people"]})["record"]
    with transaction(h.app.state.engine) as db:
        before = {person.id: (person.revision, list(person.tags or [])) for person in db.scalars(
            select(m.Person).where(m.Person.id.in_(h.seed["people"][:2])))}
    limits(h, maximum)
    body = {"item_kind": "tag.members", "items": [{"target_id": tag["id"], "expected_revision": tag["revision"],
        "payload": {"add": [{"kind": "people", "id": person} for person in h.seed["people"][:2]]}}], "reason": REASON}
    if maximum == 1:
        response = h.client("coordinator").post("/api/commands/preview", json={"kind": "bulk.apply", "payload": body})
        assert response.status_code == 422 and response.json()["error"]["code"] == "BULK_ITEM_LIMIT"
        assert h.command("coordinator", "bulk.apply", body, status=422)["error"]["code"] == "BULK_ITEM_LIMIT"
    elif maximum < 5:
        # Two member rows, two mirrored people and the tag share the same five-record ceiling.
        checked = preview(h, "coordinator", body)
        assert checked["counts"]["problem"] == 1
        assert checked["items"][0]["issues"][0]["code"] == "BULK_ITEM_LIMIT"
        assert h.command("coordinator", "bulk.apply", body, status=409)["error"]["code"] == "BULK_ITEM_LIMIT"
    else:
        applied = h.command("coordinator", "bulk.apply", body)
        assert preview(h, "coordinator", applied["undo"]["payload"])["counts"]["problem"] == 0
        h.command("coordinator", **applied["undo"])
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.TagMember).where(m.TagMember.tag_id == tag["id"])) == 0
        after = {person.id: (person.revision, list(person.tags or [])) for person in db.scalars(
            select(m.Person).where(m.Person.id.in_(before)))}
        assert {person: values[1] for person, values in after.items()} == {person: values[1] for person, values in before.items()}
        if maximum < 5:
            assert after == before
            assert db.get(m.Tag, tag["id"]).revision == tag["revision"]


@pytest.mark.parametrize("maximum", [1, 2])
def test_bulk_rank_move_counts_ended_predecessors_and_undo_uses_the_same_limit(h, maximum):
    person = h.seed["people"][0]
    category = h.configure("categories", {"name": "Synthetic counted new rank", "starts_on": "2020-01-01"})["record"]
    with transaction(h.app.state.engine) as db:
        originals = {row.id: row.ends_on for row in db.scalars(select(m.Membership).where(m.Membership.person_id == person))}
    assert len(originals) == 1
    limits(h, maximum)
    body = {"item_kind": "membership.bulk_create", "items": [{"payload": {"person_ids": [person],
        "category_id": category["id"], "starts_on": "2027-01-01", "end_other_ranks": True}}], "reason": REASON}
    checked = preview(h, "coordinator", body)
    if maximum == 1:
        assert checked["items"][0]["issues"][0]["code"] == "BULK_ITEM_LIMIT"
        assert h.command("coordinator", "bulk.apply", body, status=422)["error"]["code"] == "BULK_ITEM_LIMIT"
    else:
        assert checked["counts"]["problem"] == 0
        applied = h.command("coordinator", "bulk.apply", body)
        assert len(applied["undo"]["payload"]["items"]) == 2
        assert preview(h, "coordinator", applied["undo"]["payload"])["counts"]["problem"] == 0
        h.command("coordinator", **applied["undo"])
    with transaction(h.app.state.engine) as db:
        assert {row.id: row.ends_on for row in db.scalars(select(m.Membership).where(m.Membership.person_id == person))} == originals
