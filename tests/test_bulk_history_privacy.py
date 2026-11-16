"""Private leave text stays outside retained history; anonymisation removes legacy accidental copies only."""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from .migration_support import historical_seed


SECRET = "PRIVATE historical clinical explanation"
REASON = {"reason": "Synthetic privacy regression"}
DATABASES = ["h", pytest.param("pg", marks=pytest.mark.postgres)]


def availability(h, person=None, secret=SECRET):
    return h.command("coordinator", "availability.record", {"person_id": person or h.seed["people"][0],
        "kind": "recorded_leave", "start": "2026-10-06T08:00:00+03:00", "end": "2026-10-06T12:00:00+03:00",
        "private_reason": secret})["record"]


def apply(h, kind, row, bulk, payload=None, actor="coordinator"):
    payload = payload or REASON
    if bulk:
        return h.command(actor, "bulk.apply", {"item_kind": kind, "items": [{"target_id": row["id"],
            "expected_revision": row["revision"], "payload": payload}], **REASON})
    return h.command(actor, kind, payload, row["id"], row["revision"])


def changed_record(reply, bulk):
    return reply["items"][0]["results"][0]["record"] if bulk else reply["record"]


def undo(h, reply):
    reverse = reply["undo"]
    return h.command("coordinator", reverse["kind"], reverse["payload"],
                     reverse.get("target_id"), reverse.get("expected_revision"))


def legacy_history(h, row, *, kind="availability.update"):
    with transaction(h.app.state.engine, write=True) as db:
        snapshot = record(db.get(m.Availability, row["id"]))
        version = m.DefinitionHistory(department_id=h.seed["department_id"], kind=kind,
            object_id=row["id"], object_revision=100, snapshot=snapshot,
            actor_id=h.seed["identities"]["coordinator"], reason="Legacy snapshot retained before the privacy fix")
        db.add(version); db.flush()
        return record(version)


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("bulk", [False, True])
def test_availability_update_restore_and_undo_never_copy_private_reason_into_history(request, database, bulk):
    h = request.getfixturevalue(database)
    row = availability(h)
    changed = apply(h, "availability.update", row, bulk, {"end": "2026-10-06T13:00:00+03:00", **REASON})
    restored = undo(h, changed)
    row = changed_record(restored, bulk)
    retracted = apply(h, "availability.retract", row, bulk)
    row = changed_record(retracted, bulk)
    reply = apply(h, "availability.restore", row, bulk)
    assert SECRET not in json.dumps([changed, restored, retracted, reply])
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Availability, row["id"])
        assert stored.private_reason == SECRET and not stored.retracted
        versions = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == row["id"])))
        assert {v.kind for v in versions} == {"availability.update", "availability.undo_update", "availability.restore"}
        assert len(versions) == 3
        assert all("private_reason" not in v.snapshot for v in versions)
        assert SECRET not in json.dumps([record(v) for v in versions])


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("bulk", [False, True])
def test_anonymise_scrubs_legacy_leave_history_and_staged_payload_without_rewriting_other_evidence(request, database, bulk):
    h = request.getfixturevalue(database)
    person = h.configure("people", {"display_name": "Synthetic former colleague"})["record"]
    row = availability(h, person["id"])
    legacy = legacy_history(h, row)
    other = availability(h, h.seed["people"][1], "Another person's retained private reason")
    untouched = legacy_history(h, other)
    staged = h.command("coordinator", "bulk.stage", {"payload": {"item_kind": "availability.record", "items": [
        {"payload": {"person_id": person["id"], "kind": "recorded_leave", "start": "2026-10-07T08:00:00+03:00",
                     "end": "2026-10-07T12:00:00+03:00", "private_reason": SECRET}}], **REASON}})["staged"]
    person = h.configure("people", {"display_name": person["display_name"], "active": False},
                         person["id"], person["revision"])["record"]
    result = apply(h, "person.anonymise", person, bulk, actor="admin")
    assert SECRET not in json.dumps(result)
    with transaction(h.app.state.engine) as db:
        redacted = record(db.get(m.DefinitionHistory, legacy["id"]))
        assert redacted == {**legacy, "snapshot": {k: v for k, v in legacy["snapshot"].items() if k != "private_reason"}}
        assert record(db.get(m.DefinitionHistory, untouched["id"])) == untouched
        assert db.get(m.Availability, row["id"]).private_reason == ""
        assert db.get(m.Availability, other["id"]).private_reason == "Another person's retained private reason"
        saved = db.get(m.BulkStaged, staged["id"])
        assert saved.payload == {} and saved.status not in {"active", "waiting"}
        history = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id.in_(
            [person["id"], row["id"], staged["id"]]))))
        assert SECRET not in json.dumps([record(version) for version in history])


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("tamper", ["other_snapshot_value", "new_private_reason", "metadata", "delete"])
def test_history_privacy_exception_still_refuses_any_other_evidence_change(request, database, tamper):
    h = request.getfixturevalue(database)
    legacy = legacy_history(h, availability(h))
    with pytest.raises(DBAPIError, match="immutable evidence"), transaction(h.app.state.engine, write=True) as db:
        version = db.get(m.DefinitionHistory, legacy["id"])
        if tamper == "delete":
            db.delete(version)
        else:
            snapshot = {key: value for key, value in version.snapshot.items() if key != "private_reason"}
            if tamper == "other_snapshot_value":
                snapshot["kind"] = "off_duty"
            elif tamper == "new_private_reason":
                snapshot["private_reason"] = "A replacement private explanation"
            else:
                version.reason = "Rewrite the retained reason"
            version.snapshot = snapshot
    with transaction(h.app.state.engine) as db:
        assert record(db.get(m.DefinitionHistory, legacy["id"])) == legacy


@pytest.mark.parametrize("database", DATABASES)
def test_history_privacy_exception_does_not_apply_to_other_object_kinds(request, database):
    h = request.getfixturevalue(database)
    legacy = legacy_history(h, availability(h), kind="policy")
    with pytest.raises(DBAPIError, match="immutable evidence"), transaction(h.app.state.engine, write=True) as db:
        version = db.get(m.DefinitionHistory, legacy["id"])
        version.snapshot = {key: value for key, value in version.snapshot.items() if key != "private_reason"}


def test_privacy_guard_upgrade_removes_only_legacy_private_keys_and_is_forward_only(tmp_path):
    from alembic import command
    from apps.api.db import make_engine
    from scripts.manage import alembic_config
    path = tmp_path / "legacy-private-history.db"
    url = "sqlite:///" + str(path)
    config = alembic_config(url)
    command.upgrade(config, "0093_bulk_changes")
    engine = make_engine(url)
    try:
        with transaction(engine, write=True) as db:
            seed = historical_seed(db)
            version = m.DefinitionHistory(department_id=seed["department_id"], kind="availability.update",
                object_id=m.uid(), object_revision=1, snapshot={"private_reason": SECRET, "kind": "recorded_leave"},
                actor_id=seed["identities"]["coordinator"], reason="Legacy private history")
            db.add(version); db.flush()
            before = record(version)
        command.upgrade(config, "0098_bulk_privacy")
        command.upgrade(config, "0098_bulk_privacy")
        with transaction(engine) as db:
            assert record(db.get(m.DefinitionHistory, before["id"])) == {
                **before, "snapshot": {key: value for key, value in before["snapshot"].items() if key != "private_reason"}}
        with pytest.raises(RuntimeError, match="Forward-only"):
            command.downgrade(config, "0093_bulk_changes")
    finally:
        engine.dispose()
