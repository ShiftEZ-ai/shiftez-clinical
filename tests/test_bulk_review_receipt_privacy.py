"""An irreversible scrub must also remove personal copies and their restoration commands."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk import REASON, preview
from .governance_support import identity
from .test_request_decide_many import decide


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("operation", ["create", "update"])
def test_bulk_wish_then_anonymise_scrubs_results_receipts_replay_and_undo(request, database, operation):
    h = request.getfixturevalue(database)
    person = h.configure("people", {"display_name": "Synthetic departing preference owner", "active": False})["record"]
    old_note, new_note = "Synthetic erased old note", "Synthetic erased new note"
    payload = {"person_id": person["id"], "kind": "prefer_off", "first_day": "2026-10-04",
               "last_day": "2026-10-04", "points": 1, "note": new_note}
    item = {"item_kind": "wish." + operation, "payload": payload}
    if operation == "update":
        wish = h.command("admin", "wish.create", {**payload, "note": old_note})["wish"]
        item.update(target_id=wish["id"], expected_revision=wish["revision"])
    clinic = h.get("coordinator", "/api/catalogues")["services"][0]
    body = {"items": [item, {"item_kind": "person.anonymise", "target_id": person["id"],
        "expected_revision": person["revision"], "payload": {}},
        {"item_kind": "catalogue.update", "target_id": clinic["id"], "expected_revision": clinic["revision"],
         "payload": {"catalogue": "services", "value": {**{key: clinic[key] for key in
             ("name", "description", "starts_on", "ends_on", "active", "mode", "calendar_id", "colour")},
             "description": "Synthetic unrelated reversible edit"}}}], "reason": REASON}
    checked = preview(h, "admin", body)
    assert checked["counts"]["problem"] == 0
    assert old_note not in json.dumps(checked) and new_note not in json.dumps(checked)
    key = m.uid()
    applied = h.command("admin", "bulk.apply", body, key=key)
    assert applied["undo_partial"] and not applied["undoable"]
    assert all(part["item_kind"] == "catalogue.update" for part in applied["undo"]["payload"]["items"])
    replay = h.command("admin", "bulk.apply", body, key=key)
    recovered = h.get("admin", "/api/commands/" + key)["result"]
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.CommandReceipt, applied["receipt_id"]).result
        wish = db.scalar(select(m.StaffWish).where(m.StaffWish.person_id == person["id"]))
        assert wish.note == "" and wish.status == "open"
    for copy in (applied, replay, recovered, stored):
        assert old_note not in json.dumps(copy) and new_note not in json.dumps(copy)
    h.command("admin", **applied["undo"])
    with transaction(h.app.state.engine) as db:
        wish = db.scalar(select(m.StaffWish).where(m.StaffWish.person_id == person["id"]))
        assert wish.note == "" and wish.status == "open"
        assert db.get(m.Person, person["id"]).display_name.startswith("Former staff member")
        assert db.get(m.Service, clinic["id"]).description == clinic["description"]


@pytest.mark.parametrize("mixed", [False, True])
def test_bulk_anonymise_scrubs_aggregate_copies_by_owner_and_keeps_only_safe_inverses(h, mixed):
    secret = "Synthetic erased aggregate pattern note"
    person = h.seed["people"][0]
    value = {"person_id": person, "weekdays": [1], "start_time": "13:00", "end_time": "17:00",
             "starts_on": "2026-10-01", "note": secret}
    owned = h.command("staff", "availability_pattern.propose", value)["pattern"]
    patterns = [{**owned, "source": "pattern"}]
    if mixed:
        identity(h, "other_pattern_owner", ["staff"], person=h.seed["people"][1])
        other = h.command("other_pattern_owner", "availability_pattern.propose", {
            **value, "person_id": h.seed["people"][1], "note": "Synthetic retained unrelated note"})["pattern"]
        patterns.append({**other, "source": "pattern"})
    decision = decide(h, patterns, role="admin")
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Person, person)
        row.active = False
        revision = row.revision
    body = {"items": [{"item_kind": "request.undo", "payload": {"batch_id": decision["batch_id"]}},
        {"item_kind": "person.anonymise", "target_id": person, "expected_revision": revision, "payload": {}}],
        "reason": REASON}
    checked = preview(h, "admin", body)
    assert checked["counts"]["problem"] == 0 and secret not in json.dumps(checked)
    applied = h.command("admin", "bulk.apply", body)
    assert secret not in json.dumps(applied)
    assert bool(applied["undo"]) == applied["undo_partial"] == mixed
    if mixed:
        assert [part["target_id"] for part in applied["undo"]["payload"]["items"]] == [other["id"]]
        h.command("admin", **applied["undo"])
    with transaction(h.app.state.engine) as db:
        assert secret not in json.dumps(db.get(m.CommandReceipt, applied["receipt_id"]).result)
        assert db.get(m.AvailabilityPattern, owned["id"]).note == ""
        assert db.get(m.AvailabilityPattern, owned["id"]).status == "proposed"
        if mixed:
            assert db.get(m.AvailabilityPattern, other["id"]).status == "active"
            assert db.get(m.AvailabilityPattern, other["id"]).note == "Synthetic retained unrelated note"


def test_bulk_anonymise_scrubs_mixed_catalogue_previous_values_and_nested_undo(h):
    secret = "Synthetic erased directory detail"
    person = h.configure("people", {"display_name": secret, "title": secret, "active": False})["record"]
    other = h.configure("people", {"display_name": "Synthetic retained colleague", "title": "Retained title"})["record"]
    body = {"items": [{"item_kind": "catalogue.bulk_update", "payload": {"catalogue": "people",
        "items": [{"id": row["id"], "revision": row["revision"]} for row in (person, other)],
        "set": {"title": "Synthetic common new title"}}},
        {"item_kind": "person.anonymise", "target_id": person["id"],
         "expected_revision": person["revision"], "payload": {}}], "reason": REASON}
    checked = preview(h, "admin", body)
    assert checked["counts"]["problem"] == 0 and secret not in json.dumps(checked)
    key = m.uid()
    applied = h.command("admin", "bulk.apply", body, key=key)
    for result in (applied, h.command("admin", "bulk.apply", body, key=key)):
        assert secret not in json.dumps(result)
        reply = result["items"][0]["results"][0]
        assert reply["previous_values"][0] == {}
        assert [part["target_id"] for part in reply["undo"]["payload"]["items"]] == [other["id"]]
    with transaction(h.app.state.engine) as db:
        assert secret not in json.dumps(db.get(m.CommandReceipt, applied["receipt_id"]).result)
    h.command("admin", **applied["undo"])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, person["id"]).title == ""
        assert db.get(m.Person, other["id"]).title == other["title"]


def test_bulk_anonymise_scrubs_sign_in_copies_and_drops_reenable_inverse(h):
    secret = "Synthetic erased sign in display name"
    person = h.configure("people", {"display_name": "Synthetic departing sign in owner", "active": False})["record"]
    ident = identity(h, "departing_staff_identity", ["staff"], person=person["id"])
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Identity, ident)
        row.display_name = secret
        revision = row.revision
    body = {"items": [{"item_kind": "identity.disable", "target_id": ident,
        "expected_revision": revision, "payload": {}}, {"item_kind": "person.anonymise",
        "target_id": person["id"], "expected_revision": person["revision"], "payload": {}}], "reason": REASON}
    checked = preview(h, "admin", body)
    assert checked["counts"]["problem"] == 0 and secret not in json.dumps(checked)
    applied = h.command("admin", "bulk.apply", body)
    assert applied["undo"] is None and secret not in json.dumps(applied)
    with transaction(h.app.state.engine) as db:
        assert secret not in json.dumps(db.get(m.CommandReceipt, applied["receipt_id"]).result)
        row = db.get(m.Identity, ident)
        assert row.disabled and row.display_name.startswith("Former staff member")


def test_bulk_redaction_accepts_unhashable_ids_in_unrelated_json_attributes():
    from apps.api.services.bulk import _redact_anonymised_rows
    attributes = {"id": {"source": "external"}, "person_id": [], "undo": {"custom": True}, "previous_values": [2]}
    rows = [{"target_id": "unrelated", "record": {"id": "unrelated", "attributes": attributes}},
            {"target_id": "erased", "record": {"id": "erased", "display_name": "Erased name"}}]
    db = SimpleNamespace(info={"anonymised_records": {"erased": {"id": "erased", "display_name": "Former staff member"}}})
    _redact_anonymised_rows(db, rows)
    assert rows[0]["record"]["attributes"] == attributes
    assert rows[1]["record"]["display_name"] == "Former staff member"


@pytest.mark.parametrize("mixed", [False, True])
def test_bulk_anonymise_drops_tag_inverses_owned_by_the_erased_person(h, mixed):
    person = h.configure("people", {"display_name": "Synthetic departing tag owner", "active": False})["record"]
    tag = h.configure("tags", {"name": "Synthetic private team", "labels": ["people"]})["record"]
    refs = [{"kind": "people", "id": person["id"]}]
    if mixed:
        refs.append({"kind": "people", "id": h.seed["people"][1]})
    tag = h.command("admin", "tag.members", {"add": refs, "reason": REASON}, tag["id"], tag["revision"])["record"]
    body = {"items": [{"item_kind": "tag.members", "target_id": tag["id"],
        "expected_revision": tag["revision"], "payload": {"remove": refs}},
        {"item_kind": "person.anonymise", "target_id": person["id"],
         "expected_revision": person["revision"] + 2, "payload": {}}], "reason": REASON}
    checked = preview(h, "admin", body)
    assert checked["counts"]["problem"] == 0
    assert checked["undo_partial"] == mixed
    key = m.uid()
    applied = h.command("admin", "bulk.apply", body, key=key)
    for result in (applied, h.command("admin", "bulk.apply", body, key=key)):
        assert bool(result["undo"]) == result["undo_partial"] == mixed
        if mixed:
            assert result["undo"]["payload"]["items"][0]["payload"]["add"] == refs[1:]
    if mixed:
        h.command("admin", **applied["undo"])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, person["id"]).tags == []
        assert db.scalar(select(m.TagMember.id).where(m.TagMember.member_id == person["id"])) is None
        if mixed:
            assert db.scalar(select(m.TagMember.id).where(m.TagMember.member_id == refs[1]["id"],
                                                         m.TagMember.tag_id == tag["id"])) is not None
