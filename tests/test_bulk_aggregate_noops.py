"""Empty aggregate mutations are unchanged, without inventing irreversible changes."""
from uuid import uuid4

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from .test_bulk import REASON, preview
from .test_bulk_equivalence import case
from .test_bulk_single_commands import relationship


def unchanged_state(h):
    with transaction(h.app.state.engine) as db:
        return {cls.__tablename__: [record(row) for row in db.scalars(select(cls).order_by(cls.id))]
                for cls in (m.Membership, m.Eligibility, m.CapabilityGrant, m.Tag, m.TagMember,
                            m.Person, m.ProgrammeMembership, m.Organisation, m.DefinitionHistory)}


def noop(h, kind, scenario):
    if kind in {"eligibility.programme", "tag.members"}:
        role, command = case(h, kind)
        if kind == "tag.members":
            if scenario == "add":
                tag = h.command(role, kind, command.payload, command.target_id, command.expected_revision)["record"]
                command = command.model_copy(update={"expected_revision": tag["revision"]})
            else:
                command = command.model_copy(update={"payload": {**command.payload,
                    "add": [], "remove": command.payload["add"]}})
        else:
            value = command.payload
            h.command(role, "eligibility.create", {"person_id": value["person_ids"][0],
                "service_id": value["service_ids"][0], "starts_on": value["starts_on"], "ends_on": value["ends_on"]})
        return role, command.model_dump(mode="json", exclude_none=True)

    family = kind.split(".")[0]
    row, single, role = relationship(h, family, starts_on="2026-01-01",
        ends_on="2026-06-01" if scenario in {"partial", "rank_partial"} else None)
    if kind.endswith("undo_create"):
        ref = {"id": str(uuid4()) if scenario == "missing" else row["id"], "revision": row["revision"] + 1}
        return role, {"kind": kind, "payload": {"items": [ref], "reason": REASON}}
    key = {"membership": "category_id", "eligibility": "service_id", "grant": "capability_id"}[family]
    value = {"person_ids": [single["person_id"]], "starts_on": "2026-01-01"}
    value[key if family == "membership" else key.replace("_id", "_ids")] = (
        single[key] if family == "membership" else [single[key]])
    if family == "grant":
        value["evidence"] = single["evidence"]
    if scenario.startswith("rank_"):
        value["end_other_ranks"] = True
    if scenario == "rank_later":
        later = h.command(role, "membership.update_dates", {"starts_on": "2026-02-01", "reason": REASON},
                          row["id"], row["revision"])["record"]
        assert later["starts_on"] == "2026-02-01"
        value["category_id"] = h.configure("categories", {"name": "Unapplied rank move", "starts_on": "2020-01-01"})["record"]["id"]
    return role, {"kind": kind, "payload": value}


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("kind,scenario", [
    ("membership.bulk_create", "covered"),
    ("membership.bulk_create", "rank_covered"),
    ("membership.bulk_create", "rank_partial"),
    ("membership.bulk_create", "rank_later"),
    ("eligibility.bulk_create", "covered"),
    ("eligibility.bulk_create", "partial"),
    ("grant.bulk_create", "covered"),
    ("grant.bulk_create", "partial"),
    ("eligibility.programme", "covered"),
    ("tag.members", "add"),
    ("tag.members", "remove"),
    ("eligibility.undo_create", "stale"),
    ("eligibility.undo_create", "missing"),
    ("grant.undo_create", "stale"),
    ("grant.undo_create", "missing"),
])
def test_empty_aggregate_is_unchanged_and_not_irreversible(request, database, kind, scenario):
    h = request.getfixturevalue(database)
    role, command = noop(h, kind, scenario)
    item = {"item_kind": command.pop("kind"), **command}
    body = {"items": [item], "reason": REASON}
    before = unchanged_state(h)
    checked = preview(h, role, body)
    assert unchanged_state(h) == before
    result = h.command(role, "bulk.apply", body)
    for reply in (checked, result):
        assert reply["counts"]["unchanged"] == 1 and reply["counts"]["changed"] == 0, reply
        assert reply["items"][0]["result"] == "unchanged"
        assert reply["items"][0]["undoable"] is True
        assert reply["reversible_count"] == reply["irreversible_count"] == 0
        assert reply["undoable"] is True and reply["undo_partial"] is False
        assert reply["undo"] is None
    assert unchanged_state(h) == before


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_empty_aggregate_does_not_make_a_real_change_partially_irreversible(request, database):
    h = request.getfixturevalue(database)
    role, command = noop(h, "membership.bulk_create", "covered")
    value = command["payload"]
    body = {"item_kind": command["kind"], "items": [{"payload": value},
        {"payload": {**value, "person_ids": [h.seed["people"][1]]}}], "reason": REASON}
    before = unchanged_state(h)
    checked = preview(h, role, body)
    result = h.command(role, "bulk.apply", body)
    for reply in (checked, result):
        assert reply["counts"]["unchanged"] == 1
        assert reply["reversible_count"] == 1 and reply["irreversible_count"] == 0
        assert reply["undoable"] is True and reply["undo_partial"] is False
    assert result["counts"]["changed"] == 1
    assert len(result["undo"]["payload"]["items"]) == 1
    undone = h.command(role, **result["undo"])
    assert undone["counts"]["changed"] == 1 and undone["irreversible_count"] == 1
    assert undone["undoable"] is False  # A real deletion still has no restoration transition.
    after = unchanged_state(h)
    assert after.pop("definition_history") != before.pop("definition_history")
    assert after.pop("organisations")[0]["input_revision"] > before.pop("organisations")[0]["input_revision"]
    assert after == before
