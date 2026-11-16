"""Source ownership precedes revisions, fingerprints and diagnostic snapshots."""
import json

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .governance_support import identity
from .test_bulk import preview
from .test_requests_search import request_row


REASON = "Synthetic source authorization regression"


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)], ids=["sqlite", "postgres"])
def source_harness(request):
    return request.getfixturevalue(request.param)


def value(kind, row, payload=None):
    return {"item_kind": kind, "reason": REASON, "items": [{"target_id": row["id"],
        "expected_revision": 999, "payload": payload or {}}]}


def assert_private(check, *secrets):
    row = check["items"][0]
    assert row["issues"][0]["code"] == "FORBIDDEN", check
    assert not row.get("before") and not row.get("after") and not row.get("record")
    rendered = json.dumps(check)
    assert "current_revision" not in rendered
    assert all(secret not in rendered for secret in secrets)


def test_bulk_programme_source_authority_precedes_wrong_revision_for_every_role(source_harness):
    h = source_harness
    hidden = h.configure("programmes", {"name": "Hidden programme original name",
        "description": "Hidden programme original description", "starts_on": "2020-01-01"})["record"]
    change = value("catalogue.update", hidden, {"catalogue": "programmes", "value": {
        "name": "Caller supplied replacement", "starts_on": "2020-01-01"}})
    checks = {}
    for role in s.ROLE_NAMES:
        if role not in h.seed["identities"]:
            identity(h, role, [role], person=h.seed["people"][0])
        check = checks[role] = preview(h, role, change)
        if role in {"admin", "coordinator"}:
            assert check["items"][0]["issues"][0]["code"] == "STALE_REVISION"
        else:
            assert_private(check, hidden["name"], hidden["description"])
            failure = h.command(role, "bulk.apply", change, status=409)
            assert failure["error"]["code"] == "FORBIDDEN"
            assert hidden["name"] not in json.dumps(failure)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Programme, hidden["id"]).revision += 1
    for role, before in checks.items():
        if role not in {"admin", "coordinator"}:
            after = preview(h, role, change)
            assert before["source_fingerprint"] == after["source_fingerprint"], role


@pytest.mark.parametrize("extra_role", ["approver", "auditor"])
def test_programme_write_source_stays_scoped_with_additional_reader_roles(source_harness, extra_role):
    h = source_harness
    identity(h, "scoped_director", ["programme_lead", extra_role], person=h.seed["people"][0])
    hidden = h.configure("programmes", {"name": "Programme outside director write authority",
        "starts_on": "2020-01-01"})["record"]
    change = value("catalogue.update", hidden, {"catalogue": "programmes", "value": {
        "name": "Caller supplied name", "starts_on": "2020-01-01"}})
    assert_private(preview(h, "scoped_director", change), hidden["name"])
    single = h.command("scoped_director", "catalogue.update", {**change["items"][0]["payload"],
        "reason": REASON}, hidden["id"], 999, status=403)
    assert single["error"]["code"] == "FORBIDDEN"


def test_direct_catalogue_preview_scopes_before_fingerprint_and_revision_checks(source_harness):
    h = source_harness
    identity(h, "scoped_director", ["programme_lead"], person=h.seed["people"][0])
    hidden = h.configure("programmes", {"name": "Hidden direct preview programme",
        "starts_on": "2020-01-01"})["record"]
    response = h.client("scoped_director").post("/api/catalogue/preview", json={
        "kind": "catalogue.update", "target_id": hidden["id"], "expected_revision": 999,
        "expected_fingerprint": "0" * 64, "payload": {"catalogue": "programmes", "reason": REASON,
            "value": {"name": "Caller supplied name", "starts_on": "2020-01-01"}}})
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert hidden["name"] not in response.text and "current_revision" not in response.text


def test_restoring_restricted_role_authorizes_source_before_revision(source_harness):
    h = source_harness
    target = identity(h, "other_developer", ["operator"])
    with transaction(h.app.state.engine, write=True) as db:
        row = m.RoleBinding(department_id=h.seed["department_id"], identity_id=target,
            role="operator", starts_on=m.now().date(), revoked=True)
        db.add(row)
        db.flush()
        hidden = {"id": row.id}
    single = h.command("admin", "role.restore", {"reason": REASON}, hidden["id"], 999, status=403)
    assert single["error"]["code"] == "SYSTEM_ROLE_GRANT"
    change = value("role.restore", hidden)
    before = preview(h, "admin", change)
    assert before["items"][0]["issues"][0]["code"] == "SYSTEM_ROLE_GRANT"
    assert "current_revision" not in json.dumps(before)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.RoleBinding, hidden["id"]).revision += 1
    after = preview(h, "admin", change)
    assert before["source_fingerprint"] == after["source_fingerprint"]


@pytest.mark.parametrize("kind", ["wish.withdraw", "wish.restore", "availability_pattern.end", "availability_pattern.restore"])
def test_person_owned_source_is_authorized_before_single_and_bulk_revision(source_harness, kind):
    h = source_harness
    owner = h.seed["people"][1]
    private = "Unrelated person's private preference or pattern note"
    if kind.startswith("wish."):
        row = h.command("coordinator", "wish.create", {"person_id": owner, "first_day": "2026-11-10",
            "last_day": "2026-11-10", "kind": "prefer_off", "points": 1, "note": private})["wish"]
        cls = m.StaffWish
        if kind.endswith("restore"):
            row = h.command("coordinator", "wish.withdraw", {"reason": REASON}, row["id"], row["revision"])["wish"]
    else:
        row = h.command("coordinator", "availability_pattern.propose", {"person_id": owner, "weekdays": [1],
            "start_time": "13:00", "end_time": "17:00", "starts_on": "2026-10-01", "note": private})["pattern"]
        cls = m.AvailabilityPattern
        if kind.endswith("restore"):
            row = h.command("coordinator", "availability_pattern.end", {"reason": REASON}, row["id"], row["revision"])["pattern"]
    change = value(kind, row)
    before = preview(h, "staff", change)
    assert_private(before, private)
    single = h.command("staff", kind, {"reason": REASON}, row["id"], 999, status=403)
    assert single["error"]["code"] == "FORBIDDEN"
    with transaction(h.app.state.engine, write=True) as db:
        db.get(cls, row["id"]).revision += 1
    after = preview(h, "staff", change)
    assert_private(after, private)
    assert before["source_fingerprint"] == after["source_fingerprint"]


def test_bulk_request_source_decision_scope_precedes_revision_for_every_role(source_harness):
    h = source_harness
    with transaction(h.app.state.engine, write=True) as db:
        row = request_row(h, h.seed["people"][1], "Hidden request private reason")
        db.add(row)
        db.flush()
        hidden = {"id": row.id, "revision": row.revision}
    change = value("request.decide", hidden, {"decision": "reject"})
    checks = {}
    for role in s.ROLE_NAMES:
        if role not in h.seed["identities"]:
            identity(h, role, [role], person=h.seed["people"][0])
        check = checks[role] = preview(h, role, change)
        if role in {"admin", "approver"}:
            assert check["items"][0]["issues"][0]["code"] == "STALE_REVISION", check
        else:
            assert_private(check, "Hidden request private reason")
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.RequestRecord, hidden["id"]).revision += 1
    for role, before in checks.items():
        if role not in {"admin", "approver"}:
            after = preview(h, role, change)
            assert before["source_fingerprint"] == after["source_fingerprint"], role


@pytest.mark.parametrize("role", ["staff", "credentialer"])
def test_bulk_operational_impacts_do_not_disclose_drafts_to_excluded_readers(source_harness, role):
    h = source_harness
    workspace = h.workspace("Hidden ongoing draft outside this reader's authority")
    if role == "staff":
        source = h.get("admin", "/api/catalogues")["services"][0]
        change = value("catalogue.update", source, {"catalogue": "services", "value": {
            "name": "Caller supplied service name", "starts_on": "2020-01-01"}})
    else:
        source = h.get("admin", "/api/catalogues")["people"][0]
        service = h.configure("services", {"name": "New authorisation reference", "starts_on": "2020-01-01"})["record"]
        change = {"item_kind": "catalogue.update", "common": {"catalogue": "people"}, "reason": REASON,
            "items": [{"target_id": source["id"], "expected_revision": source["revision"], "changes": [
                {"action": "authorise", "payload": {"service_id": service["id"], "starts_on": "2027-01-01"}}]}]}
    assert h.client(role).get("/api/workspaces/" + workspace["id"]).status_code == 403
    check = preview(h, role, change)
    assert check["counts"]["problem" if role == "staff" else "ready"] == 1, check
    assert not check.get("impacts") and not check.get("warnings")
    assert workspace["name"] not in json.dumps(check) and workspace["id"] not in json.dumps(check)


@pytest.mark.parametrize("action,payload", [
    ("wish.create", {"first_day": "2026-11-10", "last_day": "2026-11-10", "kind": "prefer_off", "points": 1}),
    ("availability_pattern.propose", {"weekdays": [1], "start_time": "13:00", "end_time": "17:00", "starts_on": "2026-10-01"}),
])
def test_action_only_person_source_authorizes_person_before_wrong_revision(source_harness, action, payload):
    h = source_harness
    person_id = h.seed["people"][1]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, person_id).display_name = "Hidden raw action person source"
    change = {"item_kind": "catalogue.update", "common": {"catalogue": "people"}, "reason": REASON,
        "items": [{"target_id": person_id, "expected_revision": 999, "changes": [
            {"item_kind": action, "payload": {**payload, "person_id": person_id}}]}]}
    before = preview(h, "staff", change)
    assert_private(before, "Hidden raw action person source")
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, person_id).revision += 1
    after = preview(h, "staff", change)
    assert before["source_fingerprint"] == after["source_fingerprint"]


def test_own_preference_conflict_names_the_submitted_rota_without_disclosing_its_id(source_harness):
    h = source_harness
    workspace = h.workspace("October submitted rota")
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, workspace["id"]).status = "submitted"
    payload = {"person_id": h.seed["people"][0], "first_day": "2026-10-05", "last_day": "2026-10-05",
        "kind": "prefer_off", "points": 1}
    assert h.client("staff").get("/api/workspaces/" + workspace["id"]).status_code == 403
    single = h.command("staff", "wish.create", payload, status=409)
    assert single["error"]["code"] == "WISH_LOCKED"
    check = preview(h, "staff", {"item_kind": "wish.create", "reason": REASON, "items": [{"payload": payload}]})
    issue = check["items"][0]["issues"][0]
    assert issue["code"] == "WISH_LOCKED"
    message = f"The rota for these days ({workspace['name']}) has been submitted; preferences for them are closed"
    assert single["error"]["message"] == issue["message"] == message
    for reply in (single, check):
        assert workspace["id"] not in json.dumps(reply)


@pytest.mark.parametrize("action,payload", [
    ("wish.withdraw", {}), ("availability_pattern.end", {}), ("request.decide", {"decision": "withdraw"}),
])
def test_raw_action_source_type_mismatch_cannot_disclose_person_revision(source_harness, action, payload):
    h = source_harness
    person_id = h.seed["people"][1]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, person_id).display_name = "Hidden mismatched action person"
    change = {"item_kind": "catalogue.update", "common": {"catalogue": "people"}, "reason": REASON,
        "items": [{"target_id": person_id, "expected_revision": 999,
            "changes": [{"item_kind": action, "payload": payload}]}]}
    before = preview(h, "staff", change)
    row = before["items"][0]
    assert row["result"] == "problem" and row["issues"][0]["code"] != "STALE_REVISION"
    assert not row.get("before") and not row.get("after") and not row.get("record")
    assert "Hidden mismatched action person" not in json.dumps(before) and "current_revision" not in json.dumps(before)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, person_id).revision += 1
    after = preview(h, "staff", change)
    assert before["source_fingerprint"] == after["source_fingerprint"]


def test_actual_action_authorizes_source_when_outer_kind_is_only_a_descriptor(source_harness):
    h = source_harness
    hidden = h.command("coordinator", "availability_pattern.propose", {"person_id": h.seed["people"][1],
        "weekdays": [1], "start_time": "13:00", "end_time": "17:00", "starts_on": "2026-10-01",
        "note": "Hidden pattern in an action-only descriptor"})["pattern"]
    change = {"item_kind": "availability_pattern.propose", "reason": REASON, "items": [
        {"target_id": hidden["id"], "expected_revision": 999, "changes": [{"item_kind": "availability_pattern.end"}]}]}
    before = preview(h, "staff", change)
    assert_private(before, hidden["note"])
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.AvailabilityPattern, hidden["id"]).revision += 1
    after = preview(h, "staff", change)
    assert before["source_fingerprint"] == after["source_fingerprint"]


def test_missing_primary_kind_cannot_disclose_source_before_field_validation(source_harness):
    h = source_harness
    person_id = h.seed["people"][1]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, person_id).display_name = "Hidden malformed source person"
    change = {"common": {"catalogue": "people"}, "reason": REASON, "items": [
        {"target_id": person_id, "expected_revision": 999, "changes": [{"field": "title", "value": "Caller value"}]}]}
    before = preview(h, "staff", change)
    row = before["items"][0]
    assert row["issues"][0]["code"] == "BULK_KIND_REQUIRED"
    assert not row.get("before") and not row.get("after") and not row.get("record")
    assert "Hidden malformed source person" not in json.dumps(before) and "current_revision" not in json.dumps(before)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, person_id).revision += 1
    after = preview(h, "staff", change)
    assert before["source_fingerprint"] == after["source_fingerprint"]


def test_aggregate_catalogue_authority_precedes_snapshot_derived_schema_errors(source_harness):
    h = source_harness
    identity(h, "scoped_director", ["programme_lead"], person=h.seed["people"][0])
    hidden = h.configure("programmes", {"name": "Hidden aggregate programme name",
        "starts_on": "2030-01-01"})["record"]
    payload = {"catalogue": "programmes", "items": [{"id": hidden["id"], "revision": 999}],
        "set": {"ends_on": "2029-01-01"}, "reason": REASON}
    single = h.command("scoped_director", "catalogue.bulk_update", payload, status=403)
    assert single["error"]["code"] == "FORBIDDEN"
    change = {"item_kind": "catalogue.bulk_update", "reason": REASON, "items": [{"payload": payload}]}
    checked = preview(h, "scoped_director", change)
    assert_private(checked, hidden["name"])
    result = h.command("scoped_director", "bulk.apply", change, status=409)
    assert result["error"]["code"] == "FORBIDDEN"
    for reply in (single, checked, result):
        rendered = json.dumps(reply)
        assert hidden["name"] not in rendered and "current_revision" not in rendered


@pytest.mark.parametrize("catalogue", ["programme_memberships", "mentorships"])
@pytest.mark.parametrize("update", [False, True], ids=["create", "move"])
def test_direct_catalogue_preview_authorizes_proposed_programme_before_fingerprint(source_harness, catalogue, update):
    h = source_harness
    director = identity(h, "scoped_director", ["programme_lead"], person=h.seed["people"][0])
    hidden = h.configure("programmes", {"name": "Hidden proposed programme", "starts_on": "2020-01-01"})["record"]
    supplied = {"person_id": h.seed["people"][1], "programme_id": hidden["id"],
        "starts_on": "2026-10-01", "ends_on": "2027-01-01"}
    if catalogue == "mentorships":
        supplied["mentor_id"] = h.seed["people"][2]
    source = None
    if update:
        own = h.configure("programmes", {"name": "Owned source programme", "starts_on": "2020-01-01"})["record"]
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Programme, own["id"]).owner_identity_id = director
        source = h.configure(catalogue, {**supplied, "programme_id": own["id"]})["record"]
    command = {"kind": "catalogue.update" if update else "catalogue.create", "expected_fingerprint": "0" * 64,
        "payload": {"catalogue": catalogue, "value": supplied, "reason": REASON}}
    if source:
        command.update(target_id=source["id"], expected_revision=source["revision"])
    response = h.client("scoped_director").post("/api/catalogue/preview", json=command)
    assert response.status_code == 403 and response.json()["error"]["code"] == "FORBIDDEN", response.text
    assert hidden["name"] not in response.text and "current_revision" not in response.text


def test_pattern_undo_scopes_decisions_before_revision_and_keeps_already_waiting_state(source_harness):
    h = source_harness
    private = "Synthetic decision owner's pattern note"
    pattern = h.command("staff", "availability_pattern.propose", {"person_id": h.seed["people"][0],
        "weekdays": [1], "start_time": "13:00", "end_time": "17:00", "starts_on": "2026-10-01",
        "note": private})["pattern"]
    pattern = h.command("coordinator", "availability_pattern.decide", {"decision": "approve", "reason": REASON},
        pattern["id"], pattern["revision"])["pattern"]
    identity(h, "different_coordinator", ["coordinator"])
    assert_private(preview(h, "different_coordinator", value("availability_pattern.undo", pattern)), private)
    waiting = h.command("coordinator", "availability_pattern.undo", {"reason": REASON}, pattern["id"],
        pattern["revision"])["pattern"]
    checked = preview(h, "coordinator", {"item_kind": "availability_pattern.undo", "reason": REASON,
        "items": [{"target_id": waiting["id"], "expected_revision": waiting["revision"]}]})
    assert checked["items"][0]["issues"][0]["code"] == "WORKFLOW_STATE"
