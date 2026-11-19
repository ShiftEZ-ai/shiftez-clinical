"""Personal facts retain department authority, provenance, revisions and published inputs."""
import uuid
import copy
from datetime import date

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.snapshots import domain_snapshot
from apps.api.auth import authenticate
from apps.api.errors import Problem
from apps.api.services import scheduling_facts
from apps.worker.main import process_one
from packages.contracts import schemas as s
from packages.domain.conditions import active_rules
from tests.governance_support import identity


def workload(h, maximum=2, **more):
    return {"kind": "workload", "person_id": h.seed["people"][0], "catalogue_id": "workload_limit",
            "line": {"id": str(uuid.uuid4()), "measure": "duties", "period": {"kind": "calendar_week"},
                     "numbers": {"max": {"value": maximum, "level": "mandatory"}}},
            "reason": "Synthetic personal workload amount", **more}


def decide(h, row, action="activate", role="approver", status=200):
    family = "person.overtime" if row["kind"] == "overtime_offer" else "calendar.protection" if row["kind"] == "protected_time" else "person.workload"
    return h.command(role, family + "." + action, {"reason": "Synthetic independent decision"},
                     row["id"], row["revision"], status=status)


def replace(h, row, maximum):
    value = workload(h, maximum, replaces_id=row["id"], expected_revision=row["revision"])
    value["line"]["id"] = row["data"]["line"]["id"]
    return h.command("coordinator", "person.workload.propose", value)["fact"]


def test_fact_waits_for_independent_decision_then_undo_restores_predecessor(h):
    w = h.workspace()
    payload = workload(h)
    row = h.command("coordinator", "person.workload.propose", payload, key="one-personal-proposal")["fact"]
    again = h.command("coordinator", "person.workload.propose", payload, key="one-personal-proposal")["fact"]
    assert again == row
    with transaction(h.app.state.engine) as db:
        assert domain_snapshot(db, db.get(m.Workspace, w["id"]))["scheduling_facts"] == []
    h.command("coordinator", "person.workload.activate", {"reason": "Wrong authority"}, row["id"], row["revision"], status=403)
    row = decide(h, row)["fact"]
    own = h.get("staff", f"/api/people/{row['person_id']}/scheduling-facts")
    assert [r["id"] for r in own["active"]] == [row["id"]]
    assert own["active"][0]["data"]["line"]["measure"] == "duties"
    assert own["active"][0]["data"]["line"]["period"] == {"kind": "calendar_week"}
    with transaction(h.app.state.engine) as db:
        assert "measure" not in db.get(m.SchedulingFact, row["id"]).data["line"]
    assert any(r["policy_id"] == row["id"] and r["definition"]["rule"].get("maximum") == 2 for r in own["effective"])
    newer = replace(h, row, 4)
    newer = decide(h, newer)["fact"]
    decide(h, {**row, "revision": row["revision"] + 1}, "undo", status=409)
    decide(h, newer, "undo")
    own = h.get("coordinator", f"/api/people/{row['person_id']}/scheduling-facts")
    assert [r["id"] for r in own["active"]] == [row["id"]]
    assert {r["id"] for r in own["history"]} == {newer["id"]}
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == "scheduling_fact")))) == 7
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == "person.workload.activate"))


@pytest.mark.parametrize("role", s.ROLE_NAMES)
def test_person_fact_permissions_follow_authoritative_roles(h, role):
    if role not in h.seed["identities"]:
        identity(h, role, [role])
    payload = workload(h)
    h.command(role, "person.workload.propose", payload, status=200 if role in ("coordinator", "admin") else 403)
    readers = {"admin", "coordinator", "approver", "programme_lead", "auditor", "staff"}
    h.get(role, f"/api/people/{payload['person_id']}/scheduling-facts", status=200 if role in readers else 403)
    if role == "staff":
        h.get(role, f"/api/people/{h.seed['people'][1]}/scheduling-facts", status=403)


def test_alternate_account_for_proposer_cannot_activate(h):
    person = h.seed["people"][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["coordinator"]).person_id = person
    identity(h, "alternate_approver", ["approver"], person=person)
    row = h.command("coordinator", "person.workload.propose", workload(h))["fact"]
    assert decide(h, row, role="alternate_approver", status=403)["error"]["code"] == "SELF_APPROVAL"


def test_stale_replacements_and_duplicate_proposals_do_not_change_active_fact(h):
    row = decide(h, h.command("coordinator", "person.workload.propose", workload(h))["fact"])["fact"]
    newer = replace(h, row, 4)
    value = workload(h, 7, replaces_id=row["id"], expected_revision=row["revision"])
    value["line"]["id"] = row["data"]["line"]["id"]
    h.command("coordinator", "person.workload.propose", value, status=409)
    decide(h, row, "retire")
    assert decide(h, newer, status=409)["error"]["code"] == "STALE_REVISION"
    decide(h, newer, "decline")
    own = h.get("coordinator", f"/api/people/{row['person_id']}/scheduling-facts")
    assert own["active"] == own["proposed"] == []


def test_unreviewed_people_update_and_binding_injection_are_rejected(h):
    person = h.configure("people", {"display_name": "Synthetic personal record"})["record"]
    h.command("coordinator", "catalogue.update", {"catalogue": "people",
        "value": {"display_name": person["display_name"], "workload_facts": []}, "reason": "Attempted unsafe direct write"},
        person["id"], person["revision"], status=422)
    value = workload(h)
    value["line"]["source_bindings"] = {"max": {"policy_id": str(uuid.uuid4()), "revision": 1,
        "number_key": "max", "primitive_kind": "max_duties", "precedence_partition": "forged"}}
    h.command("coordinator", "person.workload.propose", value, status=422)
    value = workload(h)
    value["line"]["source_default"] = workload(h, 7)["line"]
    h.command("coordinator", "person.workload.propose", value, status=422)


def test_overtime_offer_keeps_pattern_and_authority(h):
    payload = {"kind": "overtime_offer", "person_id": h.seed["people"][0], "catalogue_id": "overtime_offers",
        "line": {"id": str(uuid.uuid4()), "weekdays": [0, 2], "start_time": "17:00", "end_time": "21:00",
                 "where": {"match": "all", "conditions": [{"field": "assignment_type_ids", "op": "any", "values": [h.seed["type"]]}]},
                 "numbers": {"apply": {"value": None, "level": "medium"}}},
        "reason": "Synthetic day and duty overtime offer"}
    h.command("staff", "person.overtime.propose", payload, status=403)
    row = h.command("coordinator", "person.overtime.propose", payload)["fact"]
    row = decide(h, row)["fact"]
    assert row["data"]["line"]["weekdays"] == [0, 2]
    assert row["data"]["line"]["where"] == payload["line"]["where"]
    assert row["data"]["catalogue_version"] == 1
    newer = {**payload, "on": False, "replaces_id": row["id"], "expected_revision": row["revision"]}
    row = decide(h, h.command("coordinator", "person.overtime.propose", newer)["fact"])["fact"]
    assert row["on"] is False and row["status"] == "active"


def test_calendar_description_cannot_change_protection_scope(h):
    programme = h.configure("programmes", {"name": "Synthetic teaching programme", "starts_on": "2026-01-01"})["record"]
    value = {"programme_id": programme["id"], "name": "Synthetic teaching block", "kind": "academic_block",
             "starts_on": "2026-10-04", "ends_on": "2026-10-11"}
    entry = h.configure("programme_calendar_entries", value)["record"]
    payload = {"kind": "protected_time", "calendar_entry_id": entry["id"], "catalogue_id": "protected_time",
        "line": {"id": str(uuid.uuid4()), "weekdays": [1], "start_time": "09:00", "end_time": "11:00",
                 "starts_on": value["starts_on"], "ends_on": value["ends_on"], "numbers": {"apply": {"value": None, "level": "mandatory"}}},
        "reason": "Synthetic exact academic protection"}
    row = decide(h, h.command("coordinator", "calendar.protection.propose", payload)["fact"])["fact"]
    h.get("staff", f"/api/programme-calendar-entries/{entry['id']}/protections", status=403)
    assert [r["id"] for r in h.get("auditor", f"/api/programme-calendar-entries/{entry['id']}/protections")["protections"]] == [row["id"]]
    entry = h.command("coordinator", "catalogue.update", {"catalogue": "programme_calendar_entries", "value": {**value, "name": "Updated label"},
        "reason": "Descriptive label correction"}, entry["id"], entry["revision"])["record"]
    h.command("coordinator", "catalogue.update", {"catalogue": "programme_calendar_entries", "value": {**value, "ends_on": "2026-10-12"},
        "reason": "Unreviewed clinical change"}, entry["id"], entry["revision"], status=409)
    assert row["data"]["programme_id"] == programme["id"]


def test_published_snapshot_keeps_fact_version_approved_at_publication(h):
    original = decide(h, h.command("coordinator", "person.workload.propose", workload(h, 9))["fact"])["fact"]
    w = h.publish(h.shift(h.workspace())["workspace"])["workspace"]
    decide(h, replace(h, original, 1))
    with transaction(h.app.state.engine) as db:
        data = domain_snapshot(db, db.get(m.Workspace, w["id"]))
        assert [r["id"] for r in data["scheduling_facts"] if r["status"] == "active"] == [original["id"]]


def test_other_department_fact_is_never_read_or_decided(h):
    row = h.command("coordinator", "person.workload.propose", workload(h))["fact"]
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed["organisation_id"], name="Other synthetic department")
        db.add(department)
        db.flush()
        db.get(m.SchedulingFact, row["id"]).department_id = department.id
    h.command("approver", "person.workload.activate", {"reason": "Wrong department"}, row["id"], row["revision"], status=404)
    own = h.get("coordinator", f"/api/people/{row['person_id']}/scheduling-facts")
    assert own["active"] == own["proposed"] == own["history"] == []


def activate_checked(h, row, job_id=None):
    client = h.client("approver")
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies["rota_session"], client.cookies["rota_csrf"], write=True)
        return scheduling_facts.command(db, actor, s.Command(kind="person.workload.activate", target_id=row["id"],
            expected_revision=row["revision"], payload={"reason": "Independent contextual personal review",
                "check_job_ids": [job_id] if job_id else []}), require_checks=True)


def test_native_fact_requires_exact_context_receipt_without_changing_inputs(h):
    w = h.shift(h.workspace())["workspace"]
    row = h.command("coordinator", "person.workload.propose", workload(h, 2))["fact"]
    before = h.snapshot(w)
    with pytest.raises(Problem) as error:
        activate_checked(h, row)
    assert error.value.code == "RULE_CHECK_REQUIRED"
    job = h.command("approver", "job.create", {"workspace_id": w["id"], "kind": "feasibility",
        "fact_changes": [{"fact_id": row["id"], "expected_revision": row["revision"]}]}, revision=w["revision"])["job"]
    assert process_one(h.app.state.engine)
    checked = h.get("approver", "/api/jobs/" + job["id"])["job"]
    assert checked["result"]["full_feasibility_established"], checked["result"]
    assert checked["result"]["bindings"][0]["fact_id"] == row["id"]
    assert h.snapshot(w) == before
    assert activate_checked(h, row, job["id"])["fact"]["status"] == "active"


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_person_fact_reaching_published_rota_uses_read_only_check(request, database):
    from .test_contextual_feasibility import current_window, publication_digest
    h = request.getfixturevalue(database)
    start, end = current_window(h)
    published = h.publish(h.shift(h.workspace("Published fact context", str(start), str(end)),
                                     start=f'{start}T08:00:00+03:00', end=f'{start}T12:00:00+03:00')["workspace"])
    with transaction(h.app.state.engine) as db:
        before = publication_digest(db, published["publication_id"])
    row = h.command("coordinator", "person.workload.propose", workload(h, 2))["fact"]
    job = h.command("approver", "job.create", {"workspace_id": published["workspace"]["id"], "kind": "feasibility",
        "fact_changes": [{"fact_id": row["id"], "expected_revision": row["revision"]}]},
        revision=published["workspace"]["revision"])["job"]
    assert process_one(h.app.state.engine)
    checked = h.get("approver", "/api/jobs/" + job["id"])["job"]
    assert checked["result"]["full_feasibility_established"]
    activated = activate_checked(h, row, job["id"])
    assert activated["fact"]["status"] == "active"
    assert any(item["workspace_id"] == published["workspace"]["id"] for item in activated["context_check_evidence"])
    with transaction(h.app.state.engine) as db:
        assert publication_digest(db, published["publication_id"]) == before


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
def test_native_fact_checked_activation_api_accepts_non_orm_policy_adapter(h):
    """The checked HTTP activation path must project its lightweight policy adapter without ORM inspection."""
    from dataclasses import replace
    from apps.api.main import create_app

    h.settings = replace(h.settings, generation_engine="cp_sat")
    h.app = create_app(h.settings, engine=h.app.state.engine)
    w = h.shift(h.workspace("Checked personal fact API"))["workspace"]
    row = h.command("coordinator", "person.workload.propose", workload(h, 2))["fact"]
    job = h.command("approver", "job.create", {"workspace_id": w["id"], "kind": "feasibility",
        "fact_changes": [{"fact_id": row["id"], "expected_revision": row["revision"]}]}, revision=w["revision"])["job"]
    assert process_one(h.app.state.engine)
    checked = h.get("approver", "/api/jobs/" + job["id"])["job"]
    activated = h.command("approver", "person.workload.activate", {"reason": "Independent checked API review",
        "check_job_ids": [checked["id"]]}, row["id"], row["revision"])
    assert activated["fact"]["status"] == "active"


def test_fact_replacement_preview_retires_predecessor_and_stale_receipt_blocks(h):
    w = h.shift(h.workspace())["workspace"]
    original = decide(h, h.command("coordinator", "person.workload.propose", workload(h, 1))["fact"])["fact"]
    replacement = replace(h, original, 2)
    job = h.command("approver", "job.create", {"workspace_id": w["id"], "kind": "feasibility",
        "fact_changes": [{"fact_id": replacement["id"], "expected_revision": replacement["revision"]}]}, revision=w["revision"])["job"]
    with transaction(h.app.state.engine) as db:
        proposed = db.get(m.Job, job["id"]).input_snapshot["proposed"]
        assert {r["id"]: r["status"] for r in proposed["scheduling_facts"]} == {
            original["id"]: "retired", replacement["id"]: "active"}
    assert process_one(h.app.state.engine)
    h.shift(w, start="2026-10-05T08:00:00+03:00", end="2026-10-05T12:00:00+03:00")
    with pytest.raises(Problem) as error:
        activate_checked(h, replacement, job["id"])
    assert error.value.code == "RULE_CHECK_REQUIRED"


def test_personal_fair_target_updates_only_its_exact_goal(h):
    spec = {"kind": "balance_goal", "metric": "hours", "aim": "target", "amount": 10,
            "period": "custom", "starts_on": "2026-01-01", "ends_on": "2027-12-31"}
    goal = h.policy(spec, severity="advisory")
    payload = {"kind": "target", "person_id": h.seed["people"][0], "catalogue_id": "fair_share",
        "goal_ref": {"policy_id": goal["id"], "revision": goal["revision"]},
        "line": {"id": str(uuid.uuid4()), "numbers": {"target": {"value": 7, "level": "medium"}},
                 "settings": {"balance_goal": {key: value for key, value in spec.items() if key not in ("kind", "amount")}}},
        "reason": "Synthetic personal share of the exact historical goal"}
    conflicting = copy.deepcopy(payload)
    conflicting["line"]["settings"]["balance_goal"]["metric"] = "duties"
    assert h.command("coordinator", "person.workload.propose", conflicting, status=422)["error"]["code"] == "TARGET_GOAL_SETTINGS"
    conflicting = copy.deepcopy(payload)
    conflicting["line"]["numbers"]["target"]["level"] = "high"
    assert h.command("coordinator", "person.workload.propose", conflicting, status=422)["error"]["code"] == "TARGET_GOAL_STRENGTH"
    row = decide(h, h.command("coordinator", "person.workload.propose", payload)["fact"])["fact"]
    data = h.snapshot(h.workspace())
    all_goals = [r for r in active_rules(data["policies"], context=data) if r["definition"]["rule"]["kind"] == "balance_goal"]
    [seeded] = [r for r in all_goals if r['id'] != goal['id']]
    assert seeded['definition']['rule']['metric'] == 'workload'
    assert not seeded['definition']['rule'].get('person_amounts')
    goals = [r for r in all_goals if r['id'] == goal['id']]
    assert len(goals) == 1
    assert goals[0]["id"] == goal["id"]
    assert goals[0]["definition"]["rule"]["person_amounts"] == {row["person_id"]: 7}
    assert goals[0]["definition"]["rule"]["period"] == "custom"
    assert h.command("approver", "policy.retire", {"reason": "Goal still owns a personal amount"},
        goal["id"], goal["revision"], status=409)["error"]["code"] == "GOAL_HAS_PERSONAL_TARGETS"
    retired = decide(h, row, "retire")["fact"]
    h.command("approver", "policy.retire", {"reason": "The personal amount ended first"}, goal["id"], goal["revision"])
    assert decide(h, retired, "undo", status=409)["error"]["code"] == "STALE_GOAL"


def test_personal_amount_cannot_attach_to_a_relative_fairness_goal(h):
    goal = h.policy({"kind": "balance_goal", "metric": "hours", "aim": "equal_share"}, severity="advisory")
    payload = {"kind": "target", "person_id": h.seed["people"][0], "catalogue_id": "fair_share",
        "goal_ref": {"policy_id": goal["id"], "revision": goal["revision"]},
        "line": {"id": str(uuid.uuid4()), "numbers": {"target": {"value": 7, "level": "medium"}},
                 "settings": {"balance_goal": {"metric": "hours", "aim": "target"}}},
        "reason": "A relative share cannot become a copied absolute target"}
    assert h.command("coordinator", "person.workload.propose", payload, status=422)["error"]["code"] == "TARGET_GOAL_AIM"


def test_staff_reads_only_their_own_shared_goal_amount_and_no_source_evidence(h):
    own, other = h.seed["people"][:2]
    goal = h.policy({"kind": "balance_goal", "metric": "hours", "aim": "target", "amount": 10,
        "person_amounts": {own: 7, other: 19}, "period": "rota"}, severity="advisory")
    staff = h.get("staff", f"/api/people/{own}/scheduling-facts")
    operator = h.get("coordinator", f"/api/people/{own}/scheduling-facts")
    staff_goal = next(p for p in staff["effective"] if p["policy_id"] == goal["id"])
    operator_goal = next(p for p in operator["effective"] if p["policy_id"] == goal["id"])
    assert staff_goal["definition"]["rule"]["person_amounts"] == {own: 7}
    assert operator_goal["definition"]["rule"]["person_amounts"] == {own: 7, other: 19}
    nested = {"data": {"line": {"source_bindings": {"target": {"policy_id": goal["id"]}},
        "source_default": {"settings": {"balance_goal": {"person_amounts": {own: 7, other: 19}}}},
        "settings": {"balance_goal": {"person_amounts": {own: 7, other: 19}}}}}}
    projected = scheduling_facts._personal_projection(nested, own)
    assert set(projected["data"]["line"]) == {"settings"}
    assert projected["data"]["line"]["settings"]["balance_goal"]["person_amounts"] == {own: 7}


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
def test_native_personal_target_protects_converted_goal_destination_chain(h):
    from tests.test_rule_catalogue_v2_api import proposed

    goal = h.policy({"kind": "balance_goal", "metric": "hours", "aim": "target", "amount": 10}, severity="advisory")
    w = h.workspace()
    conversion = proposed(h)
    h.command("approver", "rule.catalogue.activate", {"reason": "Reviewed full department conversion"},
        conversion["id"], conversion["revision"])
    w = h.refresh_draft_baseline(w)
    with transaction(h.app.state.engine) as db:
        current = next(r for r in db.scalars(select(m.Policy).where(m.Policy.status == "active"))
                       if r.definition["rule"].get("id") == "fair_share")
        current_id, current_revision = current.id, current.revision
        lines = copy.deepcopy(current.definition["rule"]["lines"])
    payload = {"kind": "target", "person_id": h.seed["people"][0], "catalogue_id": "fair_share",
        "goal_ref": {"policy_id": goal["id"], "revision": goal["revision"]},
        "line": {"id": str(uuid.uuid4()), "numbers": {"target": {"value": 7, "level": "medium"}},
                 "settings": {"balance_goal": {"metric": "hours", "aim": "target"}}},
        "reason": "Personal amount for the current converted source"}
    target = decide(h, h.command("coordinator", "person.workload.propose", payload)["fact"])["fact"]
    assert h.command("approver", "policy.retire", {"reason": "Would orphan its personal target"},
        current_id, current_revision, status=409)["error"]["code"] == "GOAL_HAS_PERSONAL_TARGETS"
    next(line for line in lines if any(pin['policy_id'] == goal['id']
        for pin in line.get('source_bindings', {}).values()))["numbers"]["target"]["value"] = 20
    changed = h.command("coordinator", "rule.propose", {"catalogue_id": "fair_share", "catalogue_version": 2,
        "expected_policy_id": current_id, "expected_revision": current_revision, "lines": lines,
        "reason": "Reviewed change to the converted source goal"})["record"]
    assert h.command("approver", "policy.activate", {"reason": "A linked target still pins this source"},
        changed["id"], changed["revision"], status=409)["error"]["code"] == "GOAL_HAS_PERSONAL_TARGETS"
    decide(h, target, "retire")
    changed = h.command("approver", "policy.activate", {"reason": "The personal amount ended before the source change"},
        changed["id"], changed["revision"])["record"]
    assert h.command("coordinator", "person.workload.propose", payload, status=409)["error"]["code"] == "STALE_GOAL"
    w = h.refresh_draft_baseline(w)
    current_source = next(p for p in h.snapshot(w)["policies"] if p["id"] == goal["id"])
    payload["goal_ref"]["revision"] = current_source["revision"]
    target = decide(h, h.command("coordinator", "person.workload.propose", payload)["fact"])["fact"]
    for role in ("staff", "coordinator"):
        own = h.get(role, f"/api/people/{target['person_id']}/scheduling-facts")
        resolved = next(p for p in own["effective"] if p["policy_id"] == goal["id"])
        assert resolved["revision"] == current_source["revision"]
        assert resolved["definition"]["rule"]["person_amounts"] == {target["person_id"]: 7}
    applied = h.get("coordinator", f"/api/policies/applies?person_id={target['person_id']}")
    assert next(p for p in applied["rules"] if p["policy_id"] == goal["id"])["rule"]["person_amounts"] == {
        target["person_id"]: 7}
    assert h.get("coordinator", f"/api/policies/reach?policy_id={goal['id']}")["reach"][goal["id"]] >= 1
    assert h.command("approver", "policy.retire", {"reason": "A later destination remains linked to the original source"},
        changed["id"], changed["revision"], status=409)["error"]["code"] == "GOAL_HAS_PERSONAL_TARGETS"
    assert next(p for p in h.snapshot(w)["policies"] if p["id"] == goal["id"])["definition"]["rule"]["person_amounts"] == {
        target["person_id"]: 7}


def test_one_personal_amount_per_exact_goal_can_be_replaced_or_ended(h):
    goal = h.policy({"kind": "balance_goal", "metric": "hours", "aim": "target", "amount": 10}, severity="advisory")
    payload = {"kind": "target", "person_id": h.seed["people"][0], "catalogue_id": "fair_share",
        "goal_ref": {"policy_id": goal["id"], "revision": goal["revision"]},
        "line": {"id": str(uuid.uuid4()), "numbers": {"target": {"value": 7, "level": "medium"}},
                 "settings": {"balance_goal": {"metric": "hours", "aim": "target"}}},
        "reason": "A single personal amount for one exact goal"}
    first = h.command("coordinator", "person.workload.propose", payload)["fact"]
    duplicate = copy.deepcopy(payload)
    duplicate["line"]["id"] = str(uuid.uuid4())
    duplicate["goal_ref"]["slot"] = "target"
    assert h.command("coordinator", "person.workload.propose", duplicate, status=409)["error"]["code"] == "PROPOSAL_WAITING"
    first = decide(h, first)["fact"]
    h.command("coordinator", "person.workload.propose", duplicate, status=409)
    replacement = {**payload, "replaces_id": first["id"], "expected_revision": first["revision"]}
    replacement["line"]["numbers"]["target"]["value"] = 8
    newer = decide(h, h.command("coordinator", "person.workload.propose", replacement)["fact"])["fact"]
    decide(h, newer, "retire")
    waiting = h.command("coordinator", "person.workload.propose", duplicate)["fact"]
    decide(h, waiting, "decline")
    assert h.command("coordinator", "person.workload.propose", duplicate)["fact"]["id"] != waiting["id"]


def test_converted_workload_replacement_cannot_add_an_unbound_numeric_obligation(h):
    from tests.test_rule_catalogue_v2_api import proposed

    h.policy({"kind": "max_duties", "maximum": 4, "window": "week"}, {"person_id": h.seed["people"][0]})
    conversion = proposed(h)
    h.command("approver", "rule.catalogue.activate", {"reason": "Reviewed full department conversion"},
        conversion["id"], conversion["revision"])
    with transaction(h.app.state.engine) as db:
        previous = db.scalar(select(m.SchedulingFact).where(m.SchedulingFact.kind == "workload"))
        payload = {"kind": previous.kind, "person_id": previous.person_id, "replaces_id": previous.id,
            "expected_revision": previous.revision, "reason": "An added minimum needs its own reviewed source", **copy.deepcopy(previous.data)}
    payload.pop("exception_allowed", None)
    payload["line"]["numbers"]["min"] = {"value": 1, "level": "mandatory"}
    assert h.command("coordinator", "person.workload.propose", payload, status=409)["error"]["code"] == "SOURCE_NUMBER_IMMUTABLE"


def test_published_converted_source_preserves_approved_destination_lineage_and_revision(h):
    from tests.test_rule_catalogue_v2_api import proposed

    source = h.policy({"kind": "max_duties", "maximum": 9, "window": "week"})
    conversion = proposed(h)
    h.command("approver", "rule.catalogue.activate", {"reason": "Reviewed full department conversion"},
        conversion["id"], conversion["revision"])
    with transaction(h.app.state.engine) as db:
        current = next(r for r in db.scalars(select(m.Policy).where(m.Policy.status == "active"))
                       if r.definition["rule"].get("id") == "workload_limit")
        current_id, current_revision = current.id, current.revision

    def change(maximum, ident, revision):
        with transaction(h.app.state.engine) as db:
            lines = copy.deepcopy(db.get(m.Policy, ident).definition["rule"]["lines"])
        lines[0]["numbers"]["max"]["value"] = maximum
        replacement = h.command("coordinator", "rule.propose", {"catalogue_id": "workload_limit", "catalogue_version": 2,
            "expected_policy_id": ident, "expected_revision": revision, "lines": lines,
            "reason": "A reviewed limit destination replacement"})["record"]
        return h.command("approver", "policy.activate", {"reason": "Independent destination decision"},
            replacement["id"], replacement["revision"])["record"]

    current = change(8, current_id, current_revision)
    w = h.shift(h.workspace())["workspace"]
    before = h.snapshot(w)
    original_revision = next(p for p in before["policies"] if p["id"] == source["id"])["revision"]
    assert original_revision == source["revision"] + 2
    expected = [{key: p[key] for key in ("id", "revision", "definition")}
                for p in active_rules(before["policies"], context=before)]
    published = h.publish(w)["workspace"]
    assert [{key: p[key] for key in ("id", "revision", "definition")}
            for p in active_rules(h.snapshot(published))] == expected
    change(7, current["id"], current["revision"])
    replay = h.snapshot(published)
    assert [{key: p[key] for key in ("id", "revision", "definition")}
            for p in active_rules(replay)] == expected
    assert next(p for p in replay["policies"] if p["id"] == source["id"])["revision"] == original_revision
    later = h.snapshot(h.workspace("A later draft uses the new approved destination"))
    assert next(p for p in later["policies"] if p["id"] == source["id"])["revision"] > original_revision


def test_tag_preview_resolves_complete_converted_sources_before_selecting_affected_rules(h):
    from tests.test_rule_catalogue_v2_api import proposed
    from tests.test_tags import members

    own, other = h.seed["people"][:2]
    tag = h.configure("tags", {"name": "Synthetic selected team", "labels": ["people"]})["record"]
    tag = members(h, tag, add=[("people", own)])["record"]
    tagged = h.policy({"kind": "min_rest", "hours": 11}, {"conditions": {"match": "all", "conditions": [
        {"field": "person_ids", "op": "any", "values": [tag["id"]]}]}})
    goal = h.policy({"kind": "balance_goal", "metric": "hours", "aim": "target", "amount": 10}, severity="advisory")
    conversion = proposed(h)
    h.command("approver", "rule.catalogue.activate", {"reason": "Reviewed full department conversion"},
        conversion["id"], conversion["revision"])
    target = {"kind": "target", "person_id": own, "catalogue_id": "fair_share",
        "goal_ref": {"policy_id": goal["id"], "revision": goal["revision"]},
        "line": {"id": str(uuid.uuid4()), "numbers": {"target": {"value": 7, "level": "medium"}},
                 "settings": {"balance_goal": {"metric": "hours", "aim": "target"}}},
        "reason": "A personal target whose goal does not name the changed tag"}
    decide(h, h.command("coordinator", "person.workload.propose", target)["fact"])
    response = h.client("coordinator").post(f"/api/tags/{tag['id']}/preview", json={
        "add": [{"kind": "people", "id": other}]})
    assert response.status_code == 200, response.text
    affected = next(row for row in response.json()["rules"] if row["policy_id"] == tagged["id"])
    assert (affected["people_before"], affected["people_after"]) == (1, 2)
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.TagMember.member_id).where(m.TagMember.tag_id == tag["id"]))) == [own]


def test_eligibility_gaps_reads_converted_cover_with_unrelated_native_target(h):
    from apps.api.services.catalogue import eligibility_gaps
    from tests.test_rule_catalogue_v2_api import proposed

    clinic = h.seed["services"][0]
    with transaction(h.app.state.engine, write=True) as db:
        rows = list(db.scalars(select(m.Eligibility).where(m.Eligibility.service_id == clinic)))
        expected_ids = {row.id for row in rows}
        for row in rows:
            row.ends_on = date(2026, 10, 20)
    h.policy({"kind": "minimum_cover", "minimum": 1}, {"service_ids": [clinic]})
    goal = h.policy({"kind": "balance_goal", "metric": "hours", "aim": "target", "amount": 10}, severity="advisory")
    conversion = proposed(h)
    h.command("approver", "rule.catalogue.activate", {"reason": "Reviewed full department conversion"},
        conversion["id"], conversion["revision"])
    payload = {"kind": "target", "person_id": h.seed["people"][0], "catalogue_id": "fair_share",
        "goal_ref": {"policy_id": goal["id"], "revision": goal["revision"]},
        "line": {"id": str(uuid.uuid4()), "numbers": {"target": {"value": 7, "level": "medium"}},
                 "settings": {"balance_goal": {"metric": "hours", "aim": "target"}}},
        "reason": "A personal amount beside a converted cover source"}
    decide(h, h.command("coordinator", "person.workload.propose", payload)["fact"])
    with transaction(h.app.state.engine) as db:
        assert {row["id"] for row in eligibility_gaps(db, h.seed["department_id"])} == expected_ids
