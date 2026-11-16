"""A bulk approval needs proof of its complete proposed scheduling-input union."""
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import process_one
from .test_contextual_feasibility import use_checked_activation


REASON = "Independent review of the complete synthetic bulk change"


def protected_time(h, person):
    return h.command("coordinator", "calendar.protection.propose", {
        "kind": "protected_time", "person_id": person, "catalogue_id": "protected_time",
        "line": {"id": str(uuid.uuid4()), "weekdays": [6], "start_time": "08:00", "end_time": "12:00",
                 "numbers": {"apply": {"value": None, "level": "mandatory"}}}, "reason": REASON})["fact"]


def scenario(h, minimum):
    use_checked_activation(h)
    typ = h.configure("assignment_types", {"name": "Synthetic complete approval proof", "starts_on": "2020-01-01",
        "definition": {"requirements": [{"minimum": minimum, "maximum": minimum}]}})["record"]
    workspace = h.shift(h.workspace(), typ=typ["id"], people=[])["workspace"]
    facts = [protected_time(h, person) for person in h.seed["people"][:2]]
    return workspace, facts


def checked(h, workspace, facts, *, rules=()):
    job = h.command("approver", "job.create", {"workspace_id": workspace["id"], "kind": "feasibility",
        "fact_changes": [{"fact_id": row["id"], "expected_revision": row["revision"]} for row in facts],
        "rule_changes": [{"policy_id": row["id"], "expected_revision": row["revision"]} for row in rules]},
        revision=workspace["revision"])["job"]
    assert process_one(h.app.state.engine)
    return h.get("approver", "/api/jobs/" + job["id"])["job"]


def activation(row, job):
    return {"item_kind": "calendar.protection.activate", "target_id": row["id"],
            "expected_revision": row["revision"], "payload": {"check_job_ids": [job["id"]]}}


def assert_waiting(h, facts):
    with transaction(h.app.state.engine) as db:
        assert {row.id: row.status for row in db.scalars(select(m.SchedulingFact).where(
            m.SchedulingFact.id.in_([fact["id"] for fact in facts])))} == {fact["id"]: "proposed" for fact in facts}


def test_separate_feasible_receipts_cannot_authorize_a_conflicting_bulk_union(h):
    workspace, facts = scenario(h, minimum=2)
    proofs = [checked(h, workspace, [fact]) for fact in facts]
    assert all(proof["result"]["full_feasibility_established"] for proof in proofs)
    together = checked(h, workspace, facts)
    assert together["result"]["newly_introduced_contradiction"], together["result"]
    h.command("approver", "bulk.apply", {"items": [activation(fact, proof) for fact, proof in zip(facts, proofs)],
              "reason": REASON}, status=409)
    assert_waiting(h, facts)


def test_a_combined_receipt_with_a_proven_conflict_cannot_authorize_bulk_approval(h):
    workspace, facts = scenario(h, minimum=2)
    proof = checked(h, workspace, facts)
    assert proof["result"]["newly_introduced_contradiction"], proof["result"]
    result = h.command("approver", "bulk.apply", {"items": [activation(fact, proof) for fact in facts],
                       "reason": REASON}, status=409)
    assert result["error"]["code"] == "RULE_INTRODUCES_CONFLICT"
    assert_waiting(h, facts)


def test_one_feasible_combined_receipt_authorizes_the_exact_bulk_approval(h):
    workspace, facts = scenario(h, minimum=1)
    proof = checked(h, workspace, facts)
    assert proof["result"]["full_feasibility_established"], proof["result"]
    result = h.command("approver", "bulk.apply", {"items": [activation(fact, proof) for fact in facts], "reason": REASON})
    assert result["count"] == 2
    expected_evidence = [{"workspace_id": workspace["id"], "check_job_id": proof["id"],
                          "full_feasibility_established": True}]
    assert all(row["results"][0]["context_check_evidence"] == expected_evidence for row in result["items"])
    with transaction(h.app.state.engine) as db:
        assert all(db.get(m.SchedulingFact, fact["id"]).status == "active" for fact in facts)
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "calendar.protection.activate",
            m.AuditEvent.detail["batch_id"].as_string() == result["batch_id"])))
        assert {event.target_id for event in events} == {fact["id"] for fact in facts}
        assert all(event.detail["context_check_evidence"] == expected_evidence for event in events)


def test_single_fact_activation_retains_the_same_feasibility_evidence_in_its_audit(h):
    workspace, facts = scenario(h, minimum=1)
    fact = facts[0]
    proof = checked(h, workspace, [fact])
    result = h.command("approver", "calendar.protection.activate", {"check_job_ids": [proof["id"]],
        "reason": REASON}, fact["id"], fact["revision"])
    expected_evidence = [{"workspace_id": workspace["id"], "check_job_id": proof["id"],
                          "full_feasibility_established": True}]
    assert result["context_check_evidence"] == expected_evidence
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == "calendar.protection.activate",
            m.AuditEvent.target_id == fact["id"], m.AuditEvent.detail["receipt_id"].as_string() == result["receipt_id"]))
        assert event.detail["context_check_evidence"] == expected_evidence


def test_ready_only_cannot_reuse_a_union_receipt_after_a_stale_row_is_removed(h):
    workspace, facts = scenario(h, minimum=1)
    proof = checked(h, workspace, facts)
    assert proof["result"]["full_feasibility_established"], proof["result"]
    items = [activation(fact, proof) for fact in facts]
    items[0]["expected_revision"] = 999
    result = h.command("approver", "bulk.apply", {"items": items, "apply_ready_only": True, "reason": REASON})
    assert result["count"] == 0 and result["staged_count"] == 2
    assert_waiting(h, facts)


@pytest.mark.parametrize("eligibility_first", [True, False])
@pytest.mark.parametrize("write_kind", ["eligibility.end", "availability_pattern.propose"])
def test_bulk_approval_receipt_does_not_cover_unchecked_scheduling_edits(h, eligibility_first, write_kind):
    workspace, facts = scenario(h, minimum=2)
    fact = facts[0]
    proof = checked(h, workspace, [fact])
    assert proof["result"]["full_feasibility_established"], proof["result"]
    with transaction(h.app.state.engine) as db:
        permission = db.scalar(select(m.Eligibility).where(m.Eligibility.department_id == h.seed["department_id"],
            m.Eligibility.person_id == h.seed["people"][2], m.Eligibility.service_id == h.seed["services"][0]))
        permission_id, revision = permission.id, permission.revision
        input_revision = db.get(m.Organisation, h.seed["organisation_id"]).input_revision
    if write_kind == "eligibility.end":
        write = {"item_kind": write_kind, "target_id": permission_id, "expected_revision": revision,
                 "payload": {"ends_on": "2026-10-04"}}
    else:
        write = {"item_kind": write_kind, "payload": {"person_id": h.seed["people"][2], "weekdays": [6],
            "start_time": "08:00", "end_time": "12:00", "starts_on": "2026-10-01", "note": "Synthetic weekly protection"}}
    items = [write, activation(fact, proof)] if eligibility_first else [activation(fact, proof), write]
    h.command("admin", "bulk.apply", {"items": items, "reason": REASON}, status=409)
    assert_waiting(h, facts)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Eligibility, permission_id).ends_on is None
        assert db.get(m.Organisation, h.seed["organisation_id"]).input_revision == input_revision
        assert not db.scalar(select(m.AvailabilityPattern.id).where(
            m.AvailabilityPattern.person_id == h.seed["people"][2]))


def mixed_union(h):
    workspace, facts = scenario(h, minimum=1)
    rule = h.command("coordinator", "policy.create", {"name": "Synthetic independently reviewed restriction",
        "owner": "Synthetic owner", "source": "Synthetic complete-union proof regression", "starts_on": "2020-01-01",
        "definition": {"scope": {"person_id": h.seed["people"][1]}, "rule": {"kind": "restriction"}}})["record"]
    proof = checked(h, workspace, facts[:1], rules=[rule])
    return workspace, facts[0], rule, proof


def mixed_items(fact, rule, proof, *, acknowledge=False):
    return [activation(fact, proof), {"item_kind": "policy.activate", "target_id": rule["id"],
        "expected_revision": rule["revision"], "payload": {"check_job_ids": [proof["id"]],
            "acknowledge_unresolved": acknowledge}}]


def test_mixed_fact_and_rule_union_retains_the_policy_acknowledgement_gate(h):
    _, fact, rule, proof = mixed_union(h)
    assert proof["result"]["full_feasibility_established"], proof["result"]
    assert not proof["result"]["no_new_problems"]
    assert any(f["code"] == "FEWER_ELIGIBLE_CANDIDATES" for f in proof["result"]["new_or_worse_findings"])
    result = h.command("approver", "bulk.apply", {"items": mixed_items(fact, rule, proof), "reason": REASON}, status=409)
    assert result["error"]["code"] == "RULE_CHECK_UNRESOLVED"
    assert_waiting(h, [fact])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, rule["id"]).status == "proposed"


def test_feasible_mixed_union_accepts_the_existing_explicit_policy_acknowledgement(h):
    _, fact, rule, proof = mixed_union(h)
    assert proof["result"]["full_feasibility_established"], proof["result"]
    result = h.command("approver", "bulk.apply", {"items": mixed_items(fact, rule, proof, acknowledge=True), "reason": REASON})
    assert result["count"] == 2
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, rule["id"]).status == "active"
        assert db.get(m.SchedulingFact, fact["id"]).status == "active"


def test_mixed_rule_union_cannot_use_no_new_problems_to_approve_facts_on_an_impossible_baseline(h):
    workspace, facts = scenario(h, minimum=4)
    rule = h.command("coordinator", "policy.create", {"name": "Synthetic harmless review on existing shortage",
        "owner": "Synthetic owner", "source": "Synthetic existing-shortage proof regression", "starts_on": "2020-01-01",
        "definition": {"rule": {"kind": "min_rest", "hours": 8}}})["record"]
    # This fact affects only Monday; the sole required session is on Sunday, so the existing shortage is unchanged.
    fact = h.command("coordinator", "calendar.protection.propose", {"kind": "protected_time",
        "person_id": h.seed["people"][0], "catalogue_id": "protected_time", "line": {
            "id": str(uuid.uuid4()), "weekdays": [0], "start_time": "08:00", "end_time": "12:00",
            "numbers": {"apply": {"value": None, "level": "mandatory"}}}, "reason": REASON})["fact"]
    proof = checked(h, workspace, [fact], rules=[rule])
    assert proof["result"]["pre_existing_conflict"] and not proof["result"]["full_feasibility_established"]
    assert proof["result"]["no_new_problems"], proof["result"]
    items = mixed_items(fact, rule, proof, acknowledge=True)
    items[0]["payload"]["acknowledge_unresolved"] = True
    result = h.command("approver", "bulk.apply", {"items": items, "reason": REASON}, status=409)
    assert result["error"]["code"] == "RULE_CHECK_UNRESOLVED"
    assert_waiting(h, [*facts, fact])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, rule["id"]).status == "proposed"
