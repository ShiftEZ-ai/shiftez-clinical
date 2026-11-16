"""Merged services preserve bulk lifecycle guarantees alongside residency, aliases and email privacy."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.staff_import import match_people, plan
from .test_bulk import REASON
from .test_bulk_pending_privacy import actor, anonymise
from .test_residency import setup_residency


def test_shared_import_and_bulk_matcher_keeps_exact_name_precedence_and_configured_aliases(h):
    one = h.configure("people", {"display_name": "Jos\u00e9 Exact", "staff_number": "12345", "short_code": "ONE",
                                 "rota_name": "Preferred Jos\u00e9", "rota_aliases": ["Shared alias"]})["record"]
    two = h.configure("people", {"display_name": "Other person", "short_code": "12345",
                                 "rota_aliases": ["Jose Exact", "Shared alias"]})["record"]
    with transaction(h.app.state.engine) as db:
        found = match_people(db, actor(h), ["12345", "JOSE EXACT", "preferred jose", "shared ALIAS"])
        assert [(row["id"], row["by"]) for row in found["matches"]] == [
            (one["id"], "staff_number"), (one["id"], "display_name"), (one["id"], "aliases")]
        assert {row["id"] for row in found["ambiguous"][0]["candidates"]} == {one["id"], two["id"]}
        imported = plan(db, actor(h), [(2, {"display_name": "new label", "rota_name": "PREFERRED JOSE"})])[0]
        assert imported["person"]["id"] == one["id"] and imported["person"]["by"] == "aliases"
        ambiguous = plan(db, actor(h), [(2, {"display_name": "shared alias"})])[0]
        assert "More than one person" in ambiguous["errors"]["display_name"]


def test_bulk_rotation_create_and_undo_keep_programme_scoped_history(h):
    _, programme, cohort, types, people, _ = setup_residency(h)
    created = h.command("coordinator", "bulk.apply", {
        "item_kind": "catalogue.create", "common": {"catalogue": "rotations"}, "items": [
            {"payload": {"value": {"person_id": person, "cohort_id": cohort["id"],
                "rotation_type_id": types[0]["id"], "starts_on": "2026-10-01", "ends_on": "2026-11-01"}}}
            for person in people], "reason": REASON})
    with transaction(h.app.state.engine) as db:
        rows = list(db.scalars(select(m.Rotation).where(m.Rotation.cohort_id == cohort["id"])))
        versions = list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.batch_id == created["batch_id"], m.DefinitionHistory.kind == "rotations")))
        assert len(rows) == len(versions) == 2
        assert {row.snapshot["programme_id"] for row in versions} == {programme["id"]}
    h.command("coordinator", **created["undo"])
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Rotation.id).where(m.Rotation.cohort_id == cohort["id"])) is None


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("identity_only", [False, True])
def test_anonymise_scrubs_pending_email_delivery_addresses_during_bulk(request, database, identity_only):
    h = request.getfixturevalue(database)
    person = h.configure("people", {"display_name": "Synthetic pending email owner", "active": False,
                                   "work_email": "former@example.test", "rota_name": "Former rota name",
                                   "rota_aliases": ["Former alias"]})["record"]
    with transaction(h.app.state.engine, write=True) as db:
        identity = db.get(m.Identity, h.seed["identities"]["staff"])
        identity.person_id = person["id"]
        outbox = m.Outbox(department_id=h.seed["department_id"], event_key=m.uid(), kind="email", payload={})
        db.add(outbox)
        db.flush()
        delivery = m.EmailDelivery(department_id=h.seed["department_id"], outbox_id=outbox.id,
            source_outbox_id=outbox.id, person_id=None if identity_only else person["id"],
            identity_id=identity.id if identity_only else None, sender_mode="smtp", address="former@example.test")
        db.add(delivery)
        with db.no_autoflush:
            db.info["bulk"] = {"inputs_changed": False, "availability_changed": False, "history": []}
            result = anonymise(db, h, db.get(m.Person, person["id"]))
            assert result["scrubbed"]["email_deliveries"] == 1 and delivery.address == "" and delivery.revision == 2
            assert result["record"]["work_email"] == result["record"]["rota_name"] == ""
            assert result["record"]["rota_aliases"] == []
        delivery_id = delivery.id
    with transaction(h.app.state.engine) as db:
        assert db.get(m.EmailDelivery, delivery_id).address == ""
