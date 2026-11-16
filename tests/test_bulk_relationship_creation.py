"""Every relationship create path sees earlier creates and removals in the same batch."""
from datetime import date

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.commands import dispatch
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.serialization import record
from apps.api.services import catalogue
from apps.api.services.bulk import _batch
from packages.contracts import schemas as s
from .test_bulk import REASON, preview
from .test_bulk_equivalence import case


CREATE_COMMANDS = (
    "eligibility.create", "eligibility.bulk_create", "eligibility.programme",
    "grant.create", "grant.bulk_create", "membership.create", "membership.bulk_create",
)
RELATIONSHIPS = {
    "eligibility": (m.Eligibility, "service_id"),
    "grant": (m.CapabilityGrant, "capability_id"),
    "membership": (m.Membership, "category_id"),
}
DATABASES = ["h", pytest.param("pg", marks=pytest.mark.postgres)]


def creation(h, kind):
    role, command = case(h, kind)
    family = kind.split(".")[0]
    _, key = RELATIONSHIPS[family]
    value = command.payload
    single = {"person_id": value.get("person_id") or value["person_ids"][0],
              key: value.get(key) or value[key.replace("_id", "_ids")][0],
              "starts_on": value["starts_on"], "ends_on": value["ends_on"]}
    if family == "grant":
        single["evidence"] = value["evidence"]
    return role, family, single, {"item_kind": kind, "payload": value}


def rows(h, family, value):
    cls, key = RELATIONSHIPS[family]
    with transaction(h.app.state.engine) as db:
        return [record(row) for row in db.scalars(select(cls).where(
            cls.person_id == value["person_id"], getattr(cls, key) == value[key]))]


def assert_no_creation(kind, family, coverage, result):
    if family == "membership":
        assert result["added"] == 0 and result["created_ids"] == []
        assert result["already_had_it" if coverage == "identical" else "partly_had_it"] == 1
    elif kind == "eligibility.programme":
        assert result["created_count"] == 0 and result["already_current_count"] == 1
    else:
        assert result["created"] == []
        assert result["already_current"] == (coverage == "identical")
        assert len(result["overlapping"]) == (coverage == "partial")


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("kind", CREATE_COMMANDS)
def test_every_relationship_create_replaces_a_pending_removal_and_undoes(request, database, kind):
    h = request.getfixturevalue(database)
    role, family, single, candidate = creation(h, kind)
    old = h.command(role, family + ".create", single)["record"]
    body = {"items": [
        {"item_kind": family + ".undo_create", "target_id": old["id"], "expected_revision": old["revision"],
         "payload": {"items": [{"id": old["id"], "revision": old["revision"]}]}},
        candidate,
    ], "reason": REASON}

    checked = preview(h, role, body)
    assert checked["counts"]["ready"] == 2 and checked["counts"]["problem"] == 0, checked
    assert rows(h, family, single) == [old]
    applied = h.command(role, "bulk.apply", body)
    assert applied["counts"]["changed"] == 2 and applied["counts"]["unchanged"] == 0
    replacement, = rows(h, family, single)
    assert replacement["id"] != old["id"] and replacement["revision"] == 1
    assert {field: replacement[field] for field in single} == single

    # Undo of the creation removes its replacement; the preceding creation-Undo remains irreversible.
    assert applied["undo"] is not None and applied["undo_partial"] is True
    undone = h.command(role, **applied["undo"])
    assert undone["counts"]["changed"] == 1
    assert rows(h, family, single) == []


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("kind", CREATE_COMMANDS)
@pytest.mark.parametrize("coverage", ["identical", "partial"])
def test_every_relationship_create_checks_pending_coverage(request, database, kind, coverage):
    h = request.getfixturevalue(database)
    role, family, single, candidate = creation(h, kind)
    first = {**single, **({"ends_on": "2027-03-01"} if coverage == "partial" else {})}
    body = {"items": [{"item_kind": family + ".create", "payload": first}, candidate], "reason": REASON}
    checked = preview(h, role, body)
    assert rows(h, family, single) == []

    refuses = kind.endswith(".create") or kind == "eligibility.programme" and coverage == "partial"
    if refuses:
        expected_code = "ELIGIBILITY_OVERLAP" if kind == "eligibility.programme" else "DUPLICATE_RELATIONSHIP"
        expected_message = ("A person already has a partially overlapping permission. Review that record first."
                            if kind == "eligibility.programme" else "An overlapping record already defines this relationship")
        assert checked["counts"]["ready"] == checked["counts"]["problem"] == 1, checked
        issue = checked["items"][1]["issues"][0]
        assert (issue["code"], issue["message"]) == (expected_code, expected_message)
        refused = h.command(role, "bulk.apply", body, status=409)["error"]
        assert refused["code"] == expected_code and refused["details"]["failed_index"] == 1
        assert rows(h, family, single) == []
        return

    assert checked["counts"]["ready"] == checked["counts"]["unchanged"] == 1, checked
    applied = h.command(role, "bulk.apply", body)
    assert applied["counts"]["changed"] == applied["counts"]["unchanged"] == 1
    result = applied["items"][1]["results"][0]
    assert_no_creation(kind, family, coverage, result)
    kept, = rows(h, family, single)
    assert {field: kept[field] for field in first} == first
    h.command(role, **applied["undo"])
    assert rows(h, family, single) == []


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("kind", CREATE_COMMANDS)
@pytest.mark.parametrize("coverage", ["identical", "partial"])
def test_every_relationship_create_sees_pending_rows_after_cache_preload(request, database, kind, coverage):
    h = request.getfixturevalue(database)
    role, family, single, candidate = creation(h, kind)
    cls, key = RELATIONSHIPS[family]
    actor = Actor(h.seed["identities"][role], h.seed["department_id"], h.seed["organisation_id"],
                  "Synthetic relationship creator", frozenset({role}), None, "synthetic-session")
    first = {**single, **({"ends_on": "2027-03-01"} if coverage == "partial" else {})}
    with transaction(h.app.state.engine, write=True) as db:
        with _batch(db, actor, m.uid()):
            catalogue._relationship_state(db, actor, cls, {single["person_id"]}, key, {single[key]},
                date.fromisoformat(single["starts_on"]), date.fromisoformat(single["ends_on"]))
            pending = cls(department_id=actor.department_id, **{**first,
                "starts_on": date.fromisoformat(first["starts_on"]), "ends_on": date.fromisoformat(first["ends_on"]),
                **({"granted_by": actor.identity_id} if family == "grant" else {})})
            db.add(pending)
            command = s.Command(kind=kind, payload=candidate["payload"])
            if kind.endswith(".create") or kind == "eligibility.programme" and coverage == "partial":
                with pytest.raises(Problem) as refused:
                    dispatch(db, actor, command, h.settings)
                assert refused.value.code == ("ELIGIBILITY_OVERLAP" if kind == "eligibility.programme"
                                               else "DUPLICATE_RELATIONSHIP")
            else:
                assert_no_creation(kind, family, coverage, dispatch(db, actor, command, h.settings))
            assert pending in db.new
    kept, = rows(h, family, single)
    assert kept["id"] == pending.id
    assert {field: kept[field] for field in first} == first


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("family", list(RELATIONSHIPS))
def test_bulk_relationship_creates_keep_defaults_history_audit_and_undo_with_one_flush(request, database, family):
    h = request.getfixturevalue(database)
    kind = family + ".create"
    role, _, single, _ = creation(h, kind)
    payloads = [{**single, "ends_on": "2027-03-01"}, {**single, "starts_on": "2027-03-01"}]
    body = {"item_kind": kind, "items": [{"payload": value} for value in payloads], "reason": REASON}
    checked = preview(h, role, body)
    assert checked["counts"]["ready"] == 2 and checked["counts"]["problem"] == 0
    assert rows(h, family, single) == []
    h.client(role)
    with transaction(h.app.state.engine) as db:
        revision = db.get(m.Organisation, h.seed["organisation_id"]).input_revision
    flushes = []

    def flushed(session, _context):
        if session.info.get("bulk"):
            flushes.append(1)

    event.listen(Session, "after_flush", flushed)
    try:
        applied = h.command(role, "bulk.apply", body)
    finally:
        event.remove(Session, "after_flush", flushed)
    assert flushes == [1]
    assert applied["counts"]["changed"] == 2
    returned = {item["record"]["id"]: item["record"] for item in applied["items"]}
    persisted = {row["id"]: row for row in rows(h, family, single)}
    assert persisted == returned and len(persisted) == 2
    assert all(row["revision"] == 1 and row["created_at"] for row in persisted.values())
    assert sorted((row["starts_on"], row["ends_on"]) for row in persisted.values()) == [
        ("2027-01-01", "2027-03-01"), ("2027-03-01", "2027-06-01")]
    if family == "grant":
        assert all(row["revoked"] is False and row["granted_by"] == h.seed["identities"][role]
                   and row["evidence"] == single["evidence"] for row in persisted.values())
    with transaction(h.app.state.engine) as db:
        histories = list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.object_id.in_(persisted), m.DefinitionHistory.kind == kind)))
        audits = list(db.scalars(select(m.AuditEvent).where(
            m.AuditEvent.target_id.in_(persisted), m.AuditEvent.action == kind)))
        assert {row.object_id: row.snapshot for row in histories} == persisted and len(histories) == 2
        assert len(audits) == 2 and all(row.reason == REASON for row in audits)
        assert {row.batch_id for row in histories} == {row.detail["batch_id"] for row in audits}
        assert db.get(m.Organisation, h.seed["organisation_id"]).input_revision == revision + 1
    undone = h.command(role, **applied["undo"])
    assert undone["counts"]["changed"] == 2 and rows(h, family, single) == []
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.object_id.in_(persisted), m.DefinitionHistory.kind == kind)))) == 2
