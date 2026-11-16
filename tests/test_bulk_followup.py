"""The universal Change body, nullable resolutions and honest partial Undo."""
import json
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import bulk
from packages.contracts import schemas as s
from pydantic import ValidationError
from .governance_support import identity
from .test_bulk import REASON, preview
from .test_bulk_single_commands import relationship


def apply_checked(h, role, value, check):
    response = h.client(role).post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())}, json={
        "kind": "bulk.apply", "payload": value, "expected_fingerprint": check["source_fingerprint"]})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("selection", [False, True])
def test_action_only_body_authorises_selected_people_without_a_primary_kind(h, selection):
    person = h.get("admin", "/api/catalogues")["people"][0]
    service = h.configure("services", {"name": "Universal action clinic", "starts_on": "2020-01-01"})["record"]
    source = {"selection": {"collection": "people", "include_ids": [person["id"]]}} if selection else {
        "items": [{"target_id": person["id"], "expected_revision": person["revision"]}]}
    value = {**source, "changes": [{"item_kind": "eligibility.create", "payload": {
        "service_id": service["id"], "starts_on": "2026-10-01", "ends_on": None}}], "reason": REASON}
    check = preview(h, "credentialer", value)
    assert check["counts"]["ready"] == 1, check
    reply = apply_checked(h, "credentialer", value, check)
    assert reply["undoable"] and not reply["undo_partial"]
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Eligibility.id).where(m.Eligibility.person_id == person["id"],
            m.Eligibility.service_id == service["id"]))
    h.command("credentialer", reply["undo"]["kind"], reply["undo"]["payload"])


def test_action_only_source_revision_is_checked(h):
    person = h.get("admin", "/api/catalogues")["people"][0]
    category = h.get("admin", "/api/catalogues")["categories"][0]
    value = {"items": [{"target_id": person["id"], "expected_revision": 999}],
             "changes": [{"item_kind": "membership.create", "payload": {
                 "category_id": category["id"], "starts_on": "2026-10-01"}}], "reason": REASON}
    check = preview(h, "coordinator", value)
    assert check["items"][0]["issues"][0]["code"] == "STALE_REVISION"


def test_selection_collection_is_the_source_type_even_with_a_conflicting_common_catalogue(h):
    person = h.get("admin", "/api/catalogues")["people"][0]
    value = {"item_kind": "catalogue.update", "common": {"catalogue": "services"},
             "selection": {"collection": "people", "include_ids": [person["id"]]},
             "changes": [{"field": "title", "value": "Selection-bound title"}], "reason": REASON}
    checked = preview(h, "coordinator", value)
    assert checked["counts"]["ready"] == 1, checked
    result = apply_checked(h, "coordinator", value, checked)
    assert result["undo"]["payload"]["items"][0]["payload"]["catalogue"] == "people"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, person["id"]).title == "Selection-bound title"


def test_action_only_tag_then_rank_infers_the_person_from_the_child_action(h):
    person = h.get("admin", "/api/catalogues")["people"][0]
    category = h.configure("categories", {"name": "Universal combined rank", "starts_on": "2020-01-01"})["record"]
    tag = h.configure("tags", {"name": "Universal combined tag", "labels": ["people"]})["record"]
    value = {"items": [{"target_id": person["id"], "expected_revision": person["revision"]}],
             "changes": [{"item_kind": "tag.members", "payload": {"tag_ids": [tag["id"]]}},
                         {"item_kind": "membership.create", "payload": {
                             "category_id": category["id"], "starts_on": "2026-10-01"}}], "reason": REASON}
    check = preview(h, "coordinator", value)
    assert check["counts"]["ready"] == 1, check
    result = h.command("coordinator", "bulk.apply", value)
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Membership.id).where(m.Membership.person_id == person["id"], m.Membership.category_id == category["id"]))
        assert db.scalar(select(m.TagMember.id).where(m.TagMember.member_id == person["id"], m.TagMember.tag_id == tag["id"]))
    h.command("coordinator", result["undo"]["kind"], result["undo"]["payload"])


def test_action_only_tag_changes_use_the_existing_operational_impact_check(h):
    tag = h.configure("tags", {"name": "Universal impact tag", "labels": ["people"]})["record"]
    h.policy({"kind": "restriction"}, {"tags": [tag["name"]]}, severity="mandatory")
    h.shift(h.workspace())
    value = {"selection": {"collection": "people", "include_ids": [h.seed["people"][0]]},
             "changes": [{"item_kind": "tag.members", "payload": {"tag_ids": [tag["id"]]}}], "reason": REASON}
    check = preview(h, "coordinator", value)
    assert check["counts"]["problem"] == 1 and check["warnings"]
    assert check["items"][0]["issues"][0]["message"] in {finding["message"] for finding in check["warnings"]}
    assert [option["id"] for option in check["items"][0]["issues"][0]["resolutions"]] == ["skip"]


def test_overlap_fix_keeps_nullable_later_end_and_does_not_inherit_the_earlier_field_change(h):
    first, payload, role = relationship(h, "eligibility")
    later = h.command(role, "eligibility.create", {**payload, "starts_on": "2026-12-01", "ends_on": None})["record"]
    value = {"item_kind": "eligibility.update_dates", "items": [
        {"target_id": first["id"], "expected_revision": first["revision"]} for _ in range(5)],
        "changes": [{"field": "ends_on", "how": "set", "value": "2027-01-31"}],
        "only": [4], "reason": REASON}
    initial = preview(h, role, value)
    issue = initial["items"][0]["issues"][0]
    assert issue["code"] == "DUPLICATE_RELATIONSHIP"
    option = next(option for option in issue["resolutions"] if option["id"] == "shift_later_start")
    assert option["operations"][0]["payload"] == {"starts_on": "2027-01-31", "ends_on": None}
    value["resolutions"] = {issue["group_key"]: "shift_later_start"}
    checked = preview(h, role, value)
    assert [(row["index"], row["result"]) for row in checked["items"]] == [(4, "ready")], checked
    reply = h.command(role, "bulk.apply", value)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Eligibility, first["id"]).ends_on.isoformat() == "2027-01-31"
        assert db.get(m.Eligibility, later["id"]).starts_on.isoformat() == "2027-01-31"
        assert db.get(m.Eligibility, later["id"]).ends_on is None
    h.command(role, reply["undo"]["kind"], reply["undo"]["payload"])


def test_overlap_resolution_never_offers_an_invalid_later_interval(h):
    first, payload, role = relationship(h, "eligibility")
    h.command(role, "eligibility.create", {**payload, "starts_on": "2026-12-01", "ends_on": "2027-01-31"})
    value = {"item_kind": "eligibility.update_dates", "items": [{
        "target_id": first["id"], "expected_revision": first["revision"]}],
        "changes": [{"field": "ends_on", "value": "2027-01-31"}], "reason": REASON}
    issue = preview(h, role, value)["items"][0]["issues"][0]
    assert "shift_later_start" not in {option["id"] for option in issue["resolutions"]}
    value["resolutions"] = {issue["group_key"]: "shift_later_start"}
    result = preview(h, role, value)
    assert result["items"][0]["result"] == "problem"
    assert result["items"][0]["issues"][0]["code"] == "BULK_RESOLUTION_STALE"


def test_overlap_resolution_drops_stop_before_when_the_proposed_start_is_later(h):
    first, payload, role = relationship(h, "eligibility")
    h.command(role, "eligibility.create", {**payload, "starts_on": "2026-12-01", "ends_on": None})
    value = {"item_kind": "eligibility.update_dates", "items": [{
        "target_id": first["id"], "expected_revision": first["revision"]}],
        "changes": [{"field": "starts_on", "value": "2027-01-01"},
                    {"field": "ends_on", "value": "2027-01-31"}],
        "reason": REASON}
    from apps.api.auth import authenticate
    client = h.client(role)
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies["rota_session"], client.cookies["rota_csrf"], write=True)
        with bulk._batch(db, actor, None):
            issue = bulk._relationship_resolutions(db, actor, value["items"][0], value,
                bulk._issue(Problem(409, "DUPLICATE_RELATIONSHIP", "An overlapping record already defines this relationship")))
    assert "stop_before_later" not in {option["id"] for option in issue["resolutions"]}
    assert "shift_later_start" in {option["id"] for option in issue["resolutions"]}


def test_grant_overlap_resolution_ignores_revoked_and_other_clinic_peers(h):
    first, payload, role = relationship(h, "grant")
    excluded = h.command(role, "grant.create", {**payload, "starts_on": "2026-11-02", "ends_on": "2026-11-20"})["record"]
    h.command(role, "grant.revoke", {"ends_on": "2026-11-20", "reason": REASON}, excluded["id"], excluded["revision"])
    service = h.get("admin", "/api/catalogues")["services"][0]
    h.command(role, "grant.create", {**payload, "starts_on": "2026-11-21", "ends_on": None, "service_id": service["id"]})
    actual = h.command(role, "grant.create", {**payload, "starts_on": "2026-12-01", "ends_on": None})["record"]
    value = {"item_kind": "grant.update_dates", "items": [{
        "target_id": first["id"], "expected_revision": first["revision"]}],
        "changes": [{"field": "ends_on", "value": "2027-01-31"}], "reason": REASON}
    checked = preview(h, role, value)
    issue = checked["items"][0]["issues"][0]
    assert issue["code"] == "DUPLICATE_RELATIONSHIP"
    shift = next(option for option in issue["resolutions"] if option["id"] == "shift_later_start")
    assert shift["operations"][0]["target_id"] == actual["id"]
    value["resolutions"] = {issue["group_key"]: "shift_later_start"}
    assert preview(h, role, value)["items"][0]["result"] == "ready"


def test_overlap_after_chosen_shortening_keeps_contextual_resolutions_on_the_row(h):
    first, payload, role = relationship(h, "eligibility")
    h.command(role, "eligibility.create", {**payload, "starts_on": "2026-12-01", "ends_on": None})
    value = {"item_kind": "eligibility.update_dates", "items": [{
        "target_id": first["id"], "expected_revision": first["revision"]}],
        "changes": [{"field": "starts_on", "value": "2026-10-15"},
                    {"field": "ends_on", "value": "2027-01-31"}], "reason": REASON}
    shortening = preview(h, role, value)["items"][0]["issues"][0]
    assert shortening["code"] == "WOULD_SHORTEN"
    value["resolutions"] = {shortening["group_key"]: "shorten_anyway"}
    checked = preview(h, role, value)
    issue = checked["items"][0]["issues"][0]
    assert checked["items"][0]["result"] == "problem" and issue["code"] == "DUPLICATE_RELATIONSHIP"
    assert issue["message"] == "An overlapping record already defines this relationship"
    assert {option["id"] for option in issue["resolutions"]} == {"shift_later_start", "stop_before_later", "skip"}


@pytest.mark.parametrize("error", [Problem(409, "SYNTHETIC", "Handler's resolution refusal"),
                                   ValueError("Handler's resolution value refusal"), "schema"])
def test_error_while_building_a_selected_resolution_is_kept_on_its_row(h, monkeypatch, error):
    first, payload, role = relationship(h, "eligibility")
    h.command(role, "eligibility.create", {**payload, "starts_on": "2026-12-01", "ends_on": None})
    value = {"item_kind": "eligibility.extend", "common": {"ends_on": "2027-01-31"}, "items": [{
        "target_id": first["id"], "expected_revision": first["revision"]}], "reason": REASON,
        "resolutions": {"0": "shift_later_start"}}
    if error == "schema":
        try:
            s.RelationshipUpdateDates.model_validate({"starts_on": "2027-01-31", "ends_on": "2027-01-31", "reason": REASON})
        except ValidationError as caught:
            error = caught
    def fail(*args):
        raise error
    monkeypatch.setattr(bulk, "_resolved_item", fail)
    checked = preview(h, role, value)
    assert checked["items"][0]["result"] == "problem"
    assert checked["items"][0]["issues"][0]["message"] == (
        "End date must follow start date" if isinstance(error, ValidationError) else str(error))


@pytest.mark.parametrize("kind", ["eligibility", "grant", "membership"])
@pytest.mark.parametrize("chosen", ["keep", "shorten_anyway"])
def test_open_ended_relationship_offers_end_on_that_day_or_keep_open(h, kind, chosen):
    row, _, role = relationship(h, kind, ends_on=None)
    value = {"item_kind": kind + ".update_dates", "items": [{"target_id": row["id"], "expected_revision": row["revision"]}],
             "changes": [{"field": "ends_on", "value": "2027-01-31"}], "reason": REASON}
    issue = preview(h, role, value)["items"][0]["issues"][0]
    assert issue["code"] == "WOULD_SHORTEN"
    assert [(option["id"], option["label"]) for option in issue["resolutions"]] == [
        ("shorten_anyway", "End it on that day"), ("keep", "Keep it open")]
    assert issue["resolutions"][0]["best"]
    value["resolutions"] = {issue["group_key"]: chosen}
    result = h.command(role, "bulk.apply", value)
    with transaction(h.app.state.engine) as db:
        model = {"eligibility": m.Eligibility, "grant": m.CapabilityGrant, "membership": m.Membership}[kind]
        after = db.get(model, row["id"])
        assert str(after.ends_on) == ("2027-01-31" if chosen == "shorten_anyway" else "None")
        if kind == "grant":
            assert not after.revoked
    if chosen == "shorten_anyway":
        h.command(role, result["undo"]["kind"], result["undo"]["payload"])


def test_irreversible_and_reversible_items_apply_with_a_partial_undo(h):
    inactive = h.configure("people", {"display_name": "Synthetic former person", "active": False,
                                      "title": "Synthetic removed title"})["record"]
    other = next(person for person in h.get("admin", "/api/catalogues")["people"] if person["id"] != inactive["id"])
    value = {"items": [
        {"item_kind": "person.anonymise", "target_id": inactive["id"], "expected_revision": inactive["revision"]},
        {"item_kind": "catalogue.update", "target_id": other["id"], "expected_revision": other["revision"],
         "payload": {"catalogue": "people"}, "changes": [{"field": "title", "value": "Synthetic reversible title"}]}], "reason": REASON}
    for checked in [preview(h, "admin", value), h.command("admin", "bulk.apply", value)]:
        assert checked["undoable"] is False and checked["undo_partial"] is True
        assert (checked["reversible_count"], checked["irreversible_count"]) == (1, 1)
        assert [row["undoable"] for row in checked["items"]] == [False, True]
        assert inactive["display_name"] not in json.dumps(checked)
        assert inactive["title"] not in json.dumps(checked)
    assert len(checked["undo"]["payload"]["items"]) == 1
    h.command("admin", checked["undo"]["kind"], checked["undo"]["payload"])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, inactive["id"]).display_name.startswith("Former staff member")
        assert db.get(m.Person, other["id"]).title == other["title"]


def test_anonymise_reuses_inactive_and_admin_checks_and_has_no_undo(h):
    person = h.get("admin", "/api/catalogues")["people"][0]
    value = {"item_kind": "person.anonymise", "items": [{"target_id": person["id"], "expected_revision": person["revision"]}], "reason": REASON}
    assert preview(h, "coordinator", value)["items"][0]["issues"][0]["code"] == "FORBIDDEN"
    assert preview(h, "admin", value)["items"][0]["issues"][0]["code"] == "PERSON_ACTIVE"
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, person["id"]).active = False
    checked = preview(h, "admin", value)
    assert checked["undoable"] is False and checked["undo_partial"] is False
    assert checked["undo"] is None
    result = h.command("admin", "bulk.apply", value)
    assert result["undo"] is None and result["irreversible_count"] == 1


def test_anonymise_also_redacts_an_earlier_edit_to_the_same_person_and_drops_its_reverse(h):
    person = h.configure("people", {"display_name": "Synthetic scrubbed person", "active": False,
                                    "title": "Synthetic previous title"})["record"]
    value = {"items": [
        {"item_kind": "catalogue.update", "target_id": person["id"], "expected_revision": person["revision"],
         "payload": {"catalogue": "people"}, "changes": [{"field": "title", "value": "Synthetic intermediate title"}]},
        {"item_kind": "person.anonymise", "target_id": person["id"], "expected_revision": person["revision"]}], "reason": REASON}
    result = h.command("admin", "bulk.apply", value)
    assert result["undo"] is None and not result["undo_partial"]
    assert not {person["display_name"], person["title"], "Synthetic intermediate title"} & set(json.dumps(result).split('"'))


def test_anonymise_preview_fingerprint_binds_the_person_revision(h):
    person = h.configure("people", {"display_name": "Synthetic stale former person", "active": False})["record"]
    value = {"item_kind": "person.anonymise", "items": [{"target_id": person["id"], "expected_revision": person["revision"]}], "reason": REASON}
    checked = preview(h, "admin", value)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Person, person["id"])
        row.title, row.revision = "Changed after preview", row.revision + 1
    response = h.client("admin").post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())}, json={
        "kind": "bulk.apply", "payload": value, "expected_fingerprint": checked["source_fingerprint"]})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_PREVIEW"


def test_anonymise_refuses_managed_identity_side_effects_before_preview_or_apply(h, tmp_path, monkeypatch):
    from .test_accounts import with_users_file
    with_users_file(h, tmp_path)
    person = h.configure("people", {"display_name": "Synthetic managed former person", "active": False})["record"]
    member = identity(h, "managed_former", ["staff"], person=person["id"])
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Account(identity_id=member, username="synthetic.managed.former", email="synthetic@example.test"))
    users_file = h.users
    original_users = users_file.read_text(encoding="utf-8")
    from apps.api.identity_store import AutheliaUsersFile
    def external_write(*args):
        pytest.fail("Bulk commands must not write the external identity store")
    monkeypatch.setattr(AutheliaUsersFile, "remove", external_write)
    value = {"item_kind": "person.anonymise", "items": [{"target_id": person["id"], "expected_revision": person["revision"]}], "reason": REASON}
    checked = preview(h, "admin", value)
    assert checked["items"][0]["issues"][0]["code"] == "BULK_EXTERNAL_SIDE_EFFECT"
    result = h.command("admin", "bulk.apply", value, status=409)
    assert result["error"]["code"] == "BULK_EXTERNAL_SIDE_EFFECT"
    assert users_file.read_text(encoding="utf-8") == original_users
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, person["id"]).display_name == person["display_name"]
