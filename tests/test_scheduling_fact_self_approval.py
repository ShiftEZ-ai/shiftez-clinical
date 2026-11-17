"""Scheduling-fact eligibility reflects active self-approval permissions without granting authority."""
from datetime import date

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from tests.governance_support import identity
from tests.test_requests_search import read
from tests.test_scheduling_fact_approvals import attention
from tests.test_scheduling_facts_api import workload


def grant(h, **changes):
    return h.command("admin", "self_approval.grant", {
        "identity_id": h.seed["identities"]["both"], "actions": ["rule.activate"],
        "starts_on": "2026-01-01", "ends_on": "2027-01-01", "reason": "Explicit synthetic scheduling-fact permission", **changes})["permission"]


def assert_eligibility(h, fact, role, allowed):
    listed = read(h, role)
    item = next(row for row in listed["items"] if row["id"] == fact["id"])
    assert item["can_decide"] is allowed
    assert read(h, role, id=fact["id"])["item"] == item
    assert listed["counts"]["to_decide"] == int(allowed)
    home = attention(h, role)
    if allowed:
        assert home["scheduling_facts"]["count"] == 1
        assert home["scheduling_facts"]["sample"] == [item]
    else:
        assert "scheduling_facts" not in home


@pytest.mark.parametrize("permission_state", ["absent", "wrong_action", "upcoming", "expired", "revoked", "other_identity"])
def test_own_fact_stays_undecidable_without_matching_active_permission(h, permission_state):
    identity(h, "both", ["coordinator", "approver"], person=h.seed["people"][0])
    fact = h.command("both", "person.workload.propose", workload(h))["fact"]
    if permission_state != "absent":
        changes = {"wrong_action": {"actions": ["exception.approve"]},
                   "upcoming": {"starts_on": "2099-01-01", "ends_on": "2100-01-01"},
                   "expired": {"starts_on": "2020-01-02", "ends_on": "2021-01-01"},
                   "other_identity": {"identity_id": None}}.get(permission_state, {})
        if permission_state == "other_identity":  # someone else who could use it (SW3-B-18 refuses unusable grants)
            changes["identity_id"] = identity(h, "other_both", ["coordinator", "approver"])
        permission = grant(h, **changes)
        if permission_state == "revoked":
            assert_eligibility(h, fact, "both", True)
            h.command("admin", "self_approval.revoke", {"reason": "Independent cover restored"},
                      permission["id"], permission["revision"])
    assert_eligibility(h, fact, "both", False)
    refused = h.command("both", "person.workload.activate", {"reason": "Reviewed synthetic personal amount"},
                        fact["id"], fact["revision"], status=403)
    assert refused["error"]["code"] == "SELF_APPROVAL"
    assert read(h, "both", id=fact["id"])["fact"] == fact


@pytest.mark.parametrize("permission_subject", ["identity", "role", "same_person"])
def test_permitted_self_approval_is_offered_but_requires_real_reason_and_records_use(h, permission_subject):
    person = h.seed["people"][1] if permission_subject == "same_person" else h.seed["people"][0]
    identity(h, "both", ["coordinator", "approver"], person=person)
    reviewer = "both"
    if permission_subject == "same_person":
        identity(h, "alternate", ["approver"], person=person)
        reviewer = "alternate"
    fact = h.command("both", "person.workload.propose", workload(h, person_id=person))["fact"]
    changes = {"identity_id": None, "role": "approver"} if permission_subject == "role" else {
        "identity_id": h.seed["identities"][reviewer]}
    permission = grant(h, **changes)
    assert_eligibility(h, fact, reviewer, True)
    for payload in ({}, {"reason": "xx"}, {"reason": "   "}, {"reason": "Routine update"}):
        refused = h.command(reviewer, "person.workload.activate", payload,
                            fact["id"], fact["revision"], status=422)
        assert refused["error"]["code"] == "SELF_APPROVAL_REASON"
        assert read(h, reviewer, id=fact["id"])["fact"] == fact
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == "person.workload.activate")) is None
        assert db.scalar(select(m.Outbox.id).where(m.Outbox.kind == "self_approval")) is None
    reason = "Reviewed the synthetic workload with independent cover by phone"
    activated = h.command(reviewer, "person.workload.activate", {"reason": reason},
                          fact["id"], fact["revision"])["fact"]
    assert activated["status"] == "active" and activated["approved_by"] == h.seed["identities"][reviewer]
    event = next(e for e in h.get("auditor", "/api/audit?self_approved=true")["events"]
                 if e["action"] == "person.workload.activate")
    assert event["detail"]["self_approved"] == [{"action": "rule.activate", "permission_id": permission["id"]}]
    assert event["reason"] == reason and event["target_id"] == fact["id"]
    with transaction(h.app.state.engine) as db:
        version = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == fact["id"],
            m.DefinitionHistory.object_revision == activated["revision"]))
        assert version.reason == reason and version.actor_id == activated["approved_by"]
        notice = db.scalar(select(m.Outbox).where(m.Outbox.kind == "self_approval"))
        assert notice.payload == {"action": "rule.activate", "target_id": fact["id"],
                                  "actor_id": activated["approved_by"]}
    assert read(h, reviewer)["counts"]["to_decide"] == 0
    assert "scheduling_facts" not in attention(h, reviewer)


def test_self_approval_permission_does_not_grant_approver_authority(h):
    identity(h, "both", ["coordinator"])
    fact = h.command("both", "person.workload.propose", workload(h))["fact"]
    # SW3-B-18: a coordinator cannot activate, so the grant is refused; one saved before that check still grants nothing.
    refused = h.command("admin", "self_approval.grant", {"identity_id": h.seed["identities"]["both"], "actions": ["rule.activate"],
        "starts_on": "2026-01-01", "ends_on": "2027-01-01", "reason": "Explicit synthetic scheduling-fact permission"}, status=422)
    assert refused["error"]["code"] == "SELF_APPROVAL_ACTION_UNUSABLE"
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.SelfApprovalPermission(department_id=h.seed["department_id"], created_by=h.seed["identities"]["admin"],
            identity_id=h.seed["identities"]["both"], actions=["rule.activate"], starts_on=date(2026, 1, 1),
            ends_on=date(2027, 1, 1), reason="Saved before the usability check"))
    assert_eligibility(h, fact, "both", False)
    refused = h.command("both", "person.workload.activate", {"reason": "Explicit permission alone is insufficient"},
                        fact["id"], fact["revision"], status=403)
    assert refused["error"]["code"] == "FORBIDDEN"


def test_fact_subject_needs_self_approval_for_someone_elses_proposal(h):
    reviewer = identity(h, "subject_approver", ["coordinator", "approver"], person=h.seed["people"][0])
    fact = h.command("coordinator", "person.workload.propose", workload(h))["fact"]
    assert_eligibility(h, fact, "subject_approver", False)
    refused = h.command("subject_approver", "person.workload.activate", {"reason": "Reviewed the proposal"},
                        fact["id"], fact["revision"], status=403)
    assert refused["error"]["code"] == "SELF_APPROVAL"
    permission = h.command("admin", "self_approval.grant", {"identity_id": reviewer, "actions": ["rule.activate"],
        "starts_on": "2026-01-01", "ends_on": "2027-01-01", "reason": "Explicit personal fact permission"})["permission"]
    assert_eligibility(h, fact, "subject_approver", True)
    activated = h.command("subject_approver", "person.workload.activate", {"reason": "Reviewed with independent cover"},
                          fact["id"], fact["revision"])["fact"]
    assert activated["status"] == "active"
    event = next(e for e in h.get("auditor", "/api/audit")["events"] if e["action"] == "person.workload.activate")
    assert event["detail"]["self_approved"] == [{"action": "rule.activate", "permission_id": permission["id"]}]
