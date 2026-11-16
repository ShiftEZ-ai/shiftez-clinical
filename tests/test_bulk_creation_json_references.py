"""Creation Undo never leaves stored or unflushed JSON dependencies dangling."""
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services.catalogue import _json_reference_columns, _require_unreferenced, remove_created
from apps.api.services.common import CATALOGUES
from .test_bulk import preview
from .test_bulk_single_commands import undo as undo_command
from .test_rule_catalogue_api import line


REASON = "Synthetic JSON dependency guard"


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def reference_h(request):
    return request.getfixturevalue(request.param)


def create_service(h, bulk):
    value = {"name": "Synthetic referenced clinic", "starts_on": "2020-01-01"}
    if bulk:
        result = h.command("admin", "bulk.apply", {"item_kind": "catalogue.create", "common": {"catalogue": "services"},
            "items": [{"payload": {"value": value}}], "reason": REASON})
        return result["items"][0]["record"], result["undo"]
    row = h.configure("services", value)["record"]
    return row, {"kind": "catalogue.undo_create", "target_id": row["id"], "expected_revision": row["revision"],
        "payload": {"catalogue": "services", "reason": REASON}}


def proposal(service):
    return {"catalogue_id": "duties_per_week", "lines": [line(where={"conditions": [
        {"field": "service_ids", "values": [service["id"]]}]}, numbers={"max": {"value": 3}})], "reason": REASON}


@pytest.mark.parametrize("bulk", [False, True])
def test_creation_undo_refuses_policy_json_reference_without_workspaces(reference_h, bulk):
    h = reference_h
    service, undo = create_service(h, bulk)
    policy = h.command("coordinator", "rule.propose", proposal(service))["record"]
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Workspace)) == 0
    if bulk:
        checked = preview(h, "admin", undo["payload"])
        assert checked["items"][0]["issues"][0]["code"] == "INTEGRITY_CONFLICT"
    assert undo_command(h, "admin", undo, status=409)["error"]["code"] == "INTEGRITY_CONFLICT"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Service, service["id"]).revision == service["revision"]
        assert db.get(m.Policy, policy["id"]).definition == policy["definition"]


def test_bulk_creation_undo_sees_proposal_added_after_json_cache_was_loaded(reference_h):
    h = reference_h
    first = h.configure("services", {"name": "Synthetic first removal", "starts_on": "2020-01-01"})["record"]
    second, undo = create_service(h, False)
    with transaction(h.app.state.engine) as db:
        policies = set(db.scalars(select(m.Policy.id)))
    payload = {"reason": REASON, "items": [
        {"item_kind": "catalogue.undo_create", "target_id": first["id"], "expected_revision": first["revision"],
         "payload": {"catalogue": "services"}},
        {"item_kind": "rule.propose", "payload": proposal(second)},
        {"item_kind": undo["kind"], "target_id": undo["target_id"], "expected_revision": undo["expected_revision"],
         "payload": undo["payload"]}]}
    checked = preview(h, "admin", payload)
    assert checked["items"][2]["issues"][0]["code"] == "INTEGRITY_CONFLICT"
    error = h.command("admin", "bulk.apply", payload, status=409)["error"]
    assert error["code"] == "INTEGRITY_CONFLICT" and error["details"]["failed_index"] == 2
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Service, first["id"]) is not None
        assert db.get(m.Service, second["id"]) is not None
        assert set(db.scalars(select(m.Policy.id))) == policies


@pytest.mark.parametrize("catalogue", sorted(CATALOGUES))
def test_shared_json_creation_guard_covers_every_catalogue_kind(h, catalogue):
    target = CATALOGUES[catalogue](id=m.uid())
    with Session(h.app.state.engine, autoflush=False) as db:
        db.add(m.TableView(id=m.uid(), department_id=h.seed["department_id"], owner_id=h.seed["identities"]["admin"],
            table_key=catalogue, name="Synthetic dependent view", definition={"filters": {"id": [target.id]}}))
        with pytest.raises(Problem) as error:
            _require_unreferenced(db, target)
        assert error.value.code == "INTEGRITY_CONFLICT"


@pytest.mark.parametrize("cls,column", [(cls, column) for cls, columns in _json_reference_columns().items()
                                       for column in columns], ids=lambda value: getattr(value, "key", value.__name__ if isinstance(value, type) else str(value)))
def test_shared_creation_guard_covers_pending_json_columns(h, cls, column):
    target = m.Service(id=m.uid())
    with Session(h.app.state.engine, autoflush=False) as db:
        db.add(cls(id=m.uid(), **{column.key: {"nested": [{target.id: True}]}}))
        with pytest.raises(Problem) as error:
            _require_unreferenced(db, target)
        assert error.value.code == "INTEGRITY_CONFLICT"


def test_json_creation_reference_cache_tracks_pending_edits_deletes_flushes_and_rollback(reference_h):
    h = reference_h
    with Session(h.app.state.engine, autoflush=False) as db:
        rows = [m.Service(department_id=h.seed["department_id"], name=f"Synthetic JSON target {n}",
                          starts_on=date(2020, 1, 1)) for n in range(2)]
        db.add_all(rows)
        db.flush()
        source = m.TableView(department_id=h.seed["department_id"], owner_id=h.seed["identities"]["admin"],
            table_key="services", name="Synthetic dependent view", definition={"filters": {"id": [rows[0].id]}})
        db.add(source)
        db.flush()
        db.info["bulk"] = {"test": True}
        with pytest.raises(Problem):
            _require_unreferenced(db, rows[0])
        source.definition = {"filters": {"id": [rows[1].id]}}
        _require_unreferenced(db, rows[0])
        with pytest.raises(Problem):
            _require_unreferenced(db, rows[1])
        db.flush()
        nested = db.begin_nested()
        source.definition = {}
        db.flush()
        _require_unreferenced(db, rows[1])
        nested.rollback()
        with pytest.raises(Problem):
            _require_unreferenced(db, rows[1])
        db.delete(source)
        _require_unreferenced(db, rows[1])
        db.flush()
        _require_unreferenced(db, rows[1])


def test_shared_removal_lifecycle_also_rejects_json_references(h):
    service, _ = create_service(h, False)
    actor = Actor(h.seed["identities"]["admin"], h.seed["department_id"], h.seed["organisation_id"],
                  "Synthetic administrator", frozenset({"admin"}), None, "synthetic-session")
    with Session(h.app.state.engine, autoflush=False) as db:
        db.add(m.TableView(id=m.uid(), department_id=h.seed["department_id"], owner_id=actor.identity_id,
            table_key="services", name="Synthetic dependent view", definition={"filters": {"id": [service["id"]]}}))
        with pytest.raises(Problem) as error:
            remove_created(db, actor, m.Service, "staff_import.undo", [{"id": service["id"], "revision": 1}], REASON, own=False)
        assert error.value.code == "INTEGRITY_CONFLICT"
        assert db.get(m.Service, service["id"]).revision == 1


@pytest.mark.parametrize("bulk", [False, True])
def test_creation_undo_preserves_polymorphic_source_mapping(h, bulk):
    service, undo = create_service(h, bulk)
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.SourceMapping(department_id=h.seed["department_id"], source_name="Synthetic import",
            source_key="Synthetic external clinic", target_kind="services", target_id=service["id"]))
    assert undo_command(h, "admin", undo, status=409)["error"]["code"] == "INTEGRITY_CONFLICT"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Service, service["id"]).revision == 1
        assert db.scalar(select(m.SourceMapping.id).where(m.SourceMapping.target_id == service["id"]))
