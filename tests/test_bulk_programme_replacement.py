"""Programme authorisations see the transaction's pending removals and creations."""
from datetime import date

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.commands import dispatch
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services.bulk import _batch
from .test_bulk import REASON, preview
from .test_bulk_equivalence import case


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("targeted", [False, True])
def test_programme_replacement_removes_old_permission_and_creates_full_coverage(request, database, targeted):
    h = request.getfixturevalue(database)
    role, programme = case(h, "eligibility.programme")
    payload = programme.payload
    single = {"person_id": payload["person_ids"][0], "service_id": payload["service_ids"][0],
              "starts_on": payload["starts_on"], "ends_on": payload["ends_on"]}
    old = h.command(role, "eligibility.create", single)["record"]
    assert old["revision"] == 1
    removal = {"item_kind": "eligibility.undo_create", "payload": {"items": [{"id": old["id"], "revision": 1}]}}
    if targeted:
        removal.update(target_id=old["id"], expected_revision=1)
    body = {"items": [removal, {"item_kind": "eligibility.programme", "payload": payload}], "reason": REASON}
    checked = preview(h, role, body)
    assert checked["counts"]["ready"] == 2, checked
    assert checked["items"][0]["results"][0]["removed_ids"] == [old["id"]]
    assert checked["items"][1]["results"][0]["created_count"] == 1
    assert checked["items"][1]["results"][0]["already_current_count"] == 0
    with transaction(h.app.state.engine) as db:
        assert record(db.get(m.Eligibility, old["id"])) == old
        assert len(list(db.scalars(select(m.Eligibility).where(m.Eligibility.service_id == single["service_id"])))) == 1
    applied = h.command(role, "bulk.apply", body)
    assert applied["counts"]["changed"] == 2 and applied["counts"]["unchanged"] == 0
    assert applied["items"][0]["results"][0]["removed_ids"] == [old["id"]]
    added = applied["items"][1]["results"][0]
    assert added["created_count"] == 1 and added["already_current_count"] == 0
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Eligibility, old["id"]) is None
        rows = list(db.scalars(select(m.Eligibility).where(m.Eligibility.person_id == single["person_id"],
                                                         m.Eligibility.service_id == single["service_id"])))
        assert len(rows) == 1 and rows[0].id == added["created_ids"][0] != old["id"]
        assert rows[0].revision == 1
        assert (rows[0].starts_on.isoformat(), rows[0].ends_on.isoformat()) == (single["starts_on"], single["ends_on"])
        versions = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.batch_id == applied["batch_id"])))
        assert {(row.kind, row.object_id) for row in versions} == {
            ("eligibility.undo_create", old["id"]), ("eligibility.programme", rows[0].id)}


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_programme_authorisation_counts_a_pending_creation_without_duplicating_it(request, database):
    h = request.getfixturevalue(database)
    role, command = case(h, "eligibility.programme")
    payload = command.payload
    actor = Actor(h.seed["identities"][role], h.seed["department_id"], h.seed["organisation_id"],
                  "Synthetic credentialer", frozenset({"credentialer"}), None, "synthetic-session")
    flushes = []

    def flushed(session, context):
        if session.info.get("bulk"):
            flushes.append(1)

    event.listen(Session, "after_flush", flushed)
    try:
        with transaction(h.app.state.engine, write=True) as db:
            with _batch(db, actor, m.uid()):
                pending = m.Eligibility(department_id=actor.department_id, person_id=payload["person_ids"][0],
                    service_id=payload["service_ids"][0], starts_on=date.fromisoformat(payload["starts_on"]),
                    ends_on=date.fromisoformat(payload["ends_on"]))
                db.add(pending)
                result = dispatch(db, actor, command, h.settings)
                assert result["created_count"] == 0 and result["already_current_count"] == 1
                assert flushes == []
    finally:
        event.remove(Session, "after_flush", flushed)
    assert flushes == [1]
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.Eligibility.id).where(m.Eligibility.service_id == payload["service_ids"][0]))) == [pending.id]
