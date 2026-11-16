"""Action inputs cannot redirect the records selected and labelled in the preview."""
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from .test_bulk import REASON, preview


DATABASES = ["h", pytest.param("pg", marks=pytest.mark.postgres)]


def child_action(h, name):
    catalogue, field, model, role = {
        "rank": ("categories", "category_id", m.Membership, "coordinator"),
        "authorise": ("services", "service_id", m.Eligibility, "credentialer"),
        "grant": ("capabilities", "capability_id", m.CapabilityGrant, "credentialer"),
        "programme": ("programmes", "programme_id", m.ProgrammeMembership, "coordinator"),
        "record_leave": (None, None, m.Availability, "coordinator"),
    }[name]
    if catalogue:
        ref = h.configure(catalogue, {"name": "Synthetic action reference", "starts_on": "2020-01-01"})["record"]
        payload = {field: ref["id"], "starts_on": "2027-01-01", "ends_on": "2027-02-01"}
        if name == "grant":
            payload["evidence"] = "Synthetic verified evidence"
    else:
        payload = {"kind": "recorded_leave", "start": "2027-01-01T08:00:00+03:00", "end": "2027-01-01T12:00:00+03:00",
                   "private_reason": "Synthetic recorded reason"}
    return payload, model, role


def state(h, model):
    with transaction(h.app.state.engine) as db:
        return [record(row) for row in db.scalars(select(model).order_by(model.id))]


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("name", ["rank", "authorise", "grant", "programme", "record_leave"])
def test_people_actions_reject_a_different_person_even_with_the_exact_preview_fingerprint(request, database, name):
    h = request.getfixturevalue(database)
    payload, model, role = child_action(h, name)
    a, b = h.seed["people"][:2]
    body = {"selection": {"collection": "people", "include_ids": [a]}, "reason": REASON,
            "changes": [{"action": name, "payload": {**payload, "person_id": b}}]}
    before = state(h, model)
    checked = preview(h, role, body)
    assert checked["items"][0]["target_id"] == a
    assert checked["items"][0]["result"] == "problem"
    assert checked["items"][0]["issues"][0]["code"] == "BULK_TARGET_MISMATCH"
    response = h.client(role).post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())}, json={
        "kind": "bulk.apply", "payload": body, "expected_fingerprint": checked["source_fingerprint"]})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "BULK_TARGET_MISMATCH"
    assert state(h, model) == before


@pytest.mark.parametrize("name", ["rank", "authorise", "grant", "programme", "record_leave"])
@pytest.mark.parametrize("explicit", [False, True])
def test_people_actions_fill_or_accept_the_selected_person_and_keep_foreign_references_editable(h, name, explicit):
    payload, model, role = child_action(h, name)
    person = h.seed["people"][0]
    if explicit:
        payload["person_id"] = person
    body = {"items": [{"target_id": person, "expected_revision": 1}], "common": {"catalogue": "people"},
            "changes": [{"action": name, "payload": payload}], "reason": REASON}
    assert preview(h, role, body)["items"][0]["result"] == "ready"
    result = h.command(role, "bulk.apply", body)
    child = result["items"][0]["results"][0]["record"]
    assert child["person_id"] == person
    assert all(child[key] == value for key, value in payload.items() if key.endswith("_id"))


def alias_action(h, name):
    from .test_availability_patterns import pattern
    from .test_rule_catalogue_api import approve, line, propose
    from .test_scheduling_facts_api import decide, workload
    from .test_staff_wishes import wish
    a, b = h.seed["people"][:2]
    role = "coordinator"
    if name == "wish":
        row = wish(h, "2027-01-05", role=role)["wish"]
        kind, payload, model = "wish.update", {"person_id": b, "first_day": "2027-01-05",
            "last_day": "2027-01-05", "kind": "prefer_off", "points": 1}, m.StaffWish
    elif name == "pattern":
        row = h.command(role, "availability_pattern.propose", pattern(a))["pattern"]
        kind, payload, model = "availability_pattern.propose", pattern(b), m.AvailabilityPattern
    elif name == "role":
        role = "admin"
        row = h.command(role, "role.grant", {"identity_id": h.seed["identities"]["auditor"],
            "role": "programme_lead", "starts_on": "2020-01-01", "reason": REASON})["record"]
        kind, payload, model = "role.grant", {"identity_id": h.seed["identities"]["staff"],
            "role": "coordinator", "starts_on": "2020-01-01"}, m.RoleBinding
    elif name == "rule":
        row = approve(h, propose(h, "duties_per_week", [line(numbers={"max": {"value": 5}})])["record"])
        kind, payload, model = "rule.propose", {"catalogue_id": "duties_per_month",
            "lines": [line(numbers={"max": {"value": 20}})]}, m.Policy
    elif name == "fact":
        row = decide(h, h.command(role, "person.workload.propose", workload(h))["fact"])["fact"]
        kind, payload, model = "person.workload.propose", workload(h, person_id=b), m.SchedulingFact
    elif name == "exception":
        policy = h.policy({"kind": "max_duties", "maximum": 5}, exception=True)
        payload = {"person_id": a, "policy_id": policy["id"], "starts_on": "2027-01-01", "ends_on": "2027-02-01", "reason": REASON}
        row = h.command(role, "exception.create", payload)["record"]
        kind, payload, model = "exception.create", {**payload, "person_id": b}, m.PolicyException
    else:
        first = h.shift(h.workspace("Selected synthetic rota"))
        second = h.shift(h.workspace("Other synthetic rota"))
        row = first["workspace"]
        kind, payload, model = "shift.action", {"workspace_id": second["workspace"]["id"],
            "shift_ids": [second["shifts"][0]["id"]], "action": "cancel"}, m.Shift
    return role, row, kind, payload, model


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("name", ["wish", "pattern", "role", "rule", "fact", "exception", "workspace"])
def test_action_owner_and_target_aliases_cannot_redirect_a_selected_record(request, database, name):
    h = request.getfixturevalue(database)
    role, row, kind, payload, model = alias_action(h, name)
    body = {"items": [{"target_id": row["id"], "expected_revision": row["revision"],
                      "changes": [{"item_kind": kind, "payload": payload}]}], "reason": REASON}
    before = state(h, model)
    checked = preview(h, role, body)
    assert checked["items"][0]["result"] == "problem", checked
    assert checked["items"][0]["issues"][0]["code"] == "BULK_TARGET_MISMATCH", checked
    refused = h.command(role, "bulk.apply", body, status=409)["error"]
    assert refused["code"] == "BULK_TARGET_MISMATCH"
    assert state(h, model) == before


@pytest.mark.parametrize("alias", ["workspace_id", "shift_ids"])
def test_selected_session_actions_reject_explicit_aliases_before_lowering(h, alias):
    first = h.shift(h.workspace("Selected session rota"))
    second = h.shift(h.workspace("Other session rota"))
    wrong = second["workspace"]["id"] if alias == "workspace_id" else [second["shifts"][0]["id"]]
    body = {"selection": {"collection": "sessions", "include_ids": [first["shifts"][0]["id"]]},
        "changes": [{"item_kind": "shift.action", "payload": {"action": "cancel", alias: wrong}}], "reason": REASON}
    before = state(h, m.Shift)
    checked = preview(h, "coordinator", body)
    assert checked["items"][0]["issues"][0]["code"] == "BULK_TARGET_MISMATCH"
    assert state(h, m.Shift) == before


@pytest.mark.parametrize("name", ["wish", "pattern", "role", "rule", "fact", "exception", "workspace"])
def test_actions_accept_matching_subjects_and_fill_missing_target_aliases(h, name):
    role, row, kind, payload, _ = alias_action(h, name)
    if name in {"wish", "pattern", "fact", "exception"}:
        payload["person_id"] = row["person_id"]
    if name == "role":
        payload["identity_id"] = row["identity_id"]
    elif name == "rule":
        payload["catalogue_id"] = row["definition"]["rule"]["id"]
    elif name == "fact":
        payload["line"]["id"] = row["data"]["line"]["id"]
    elif name == "exception":
        payload.update(starts_on="2027-02-01", ends_on="2027-03-01")
    elif name == "workspace":
        payload.update(workspace_id=row["id"], shift_ids=[h.detail(row)["shifts"][0]["id"]])
    body = {"items": [{"target_id": row["id"], "expected_revision": row["revision"],
                      "changes": [{"item_kind": kind, "payload": payload}]}], "reason": REASON}
    checked = preview(h, role, body)
    assert checked["items"][0]["result"] in {"ready", "waiting", "unchanged"}, checked
    h.command(role, "bulk.apply", body)


def test_programme_action_keeps_its_fixed_catalogue_and_selected_person(h):
    payload, _, role = child_action(h, "programme")
    for wrong in ({"catalogue": "mentorships"}, {"value": {"person_id": h.seed["people"][1]}}):
        body = {"selection": {"collection": "people", "include_ids": [h.seed["people"][0]]},
            "changes": [{"action": "programme", "payload": {**payload, **wrong}}], "reason": REASON}
        assert preview(h, role, body)["items"][0]["issues"][0]["code"] == "BULK_TARGET_MISMATCH"


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("family", ["rule", "fact"])
def test_implicit_proposal_revision_tracks_the_selected_records_prior_batch_transition(request, database, family):
    from .governance_support import identity
    from .test_rule_catalogue_api import line, propose
    from .test_scheduling_facts_api import workload
    h = request.getfixturevalue(database)
    identity(h, "independent_editor", ["coordinator", "approver"])
    if family == "rule":
        row = propose(h, "duties_per_week", [line(numbers={"max": {"value": 5}})])["record"]
        activate, proposal = "policy.activate", "rule.propose"
        payload = {"catalogue_id": "duties_per_week", "lines": [line(numbers={"max": {"value": 4}})]}
        model = m.Policy
    else:
        payload = workload(h)
        row = h.command("coordinator", "person.workload.propose", payload)["fact"]
        activate, proposal = "person.workload.activate", "person.workload.propose"
        payload["line"]["numbers"]["max"]["value"] = 4
        model = m.SchedulingFact
    body = {"items": [
        {"item_kind": activate, "target_id": row["id"], "expected_revision": row["revision"], "payload": {}},
        {"item_kind": proposal, "target_id": row["id"], "expected_revision": row["revision"], "payload": payload}
    ], "reason": REASON}
    checked = preview(h, "independent_editor", body)
    assert checked["counts"]["problem"] == 0, checked
    result = h.command("independent_editor", "bulk.apply", body)
    assert result["items"][1]["result"] == "waiting"
    with transaction(h.app.state.engine) as db:
        assert db.get(model, row["id"]).status == "active"
        assert len([record for record in db.scalars(select(model)) if record.status == "proposed"]) == 1
