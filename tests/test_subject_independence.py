"""Decision independence follows the subject, while authorship remains in the audit trail."""
import uuid

import pytest

from .governance_support import identity
from .test_request_decide_many import decide as decide_many, request_row
from .test_requests_for_me import read
from .test_rc17_rotation_swap import propose, swap
from .test_scheduling_fact_self_approval import assert_eligibility
from .test_scheduling_fact_approvals import calendar_entry
from .test_overtime_volunteer import offer, offer_rule
from .test_scheduling_facts_api import workload


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def database(request):
    return request.getfixturevalue(request.param)


def assert_waiting(h, role, rows):
    page = read(h, role, for_me=1, limit=100)
    assert {item["id"] for item in page["items"]} == {row["id"] for row in rows}
    assert all(item["can_decide"] for item in page["items"])
    assert page["total"] == page["counts"]["to_decide"] == page["counts"]["stages"]["waiting"] == len(rows)
    home = {item["key"]: item for item in h.get(role, "/api/home")["attention"]}
    assert home.get("requests", {}).get("count", 0) == len(rows)


@pytest.mark.parametrize("linked", [False, True])
def test_entering_for_someone_else_allows_decision_queue_badge_and_home(database, linked):
    h = database
    reviewer = identity(h, "request_reviewer", ["coordinator", "approver"],
                        person=h.seed["people"][0] if linked else None)
    row = request_row(h, "request_reviewer", h.seed["people"][1], kind="meeting")
    assert_waiting(h, "request_reviewer", [row])
    decided = h.command("request_reviewer", "request.decide", {"decision": "approve"},
                        row["id"], row["revision"])["record"]
    assert decided["status"] == "approved"
    assert decided["created_by"] == decided["decided_by"] == reviewer
    detail = read(h, "request_reviewer", id=row["id"])
    assert detail["history"][0]["actor"] == detail["history"][-1]["actor"] == "Fictional request_reviewer"
    event = next(e for e in h.get("auditor", "/api/audit")["events"] if e["action"] == "request.decide")
    assert event["actor_id"] == reviewer and "self_approved" not in event["detail"]
    assert_waiting(h, "request_reviewer", [])


@pytest.mark.parametrize("entered_by", ["coordinator", "request_reviewer"])
def test_self_subject_requires_permission_real_reason_and_audit_regardless_of_author(database, entered_by):
    h = database
    person = h.seed["people"][0]
    identity(h, "request_reviewer", ["coordinator", "approver"], person=person)
    reviewer = identity(h, "alternate_reviewer", ["coordinator", "approver"], person=person)
    row = request_row(h, entered_by, person, kind="meeting")
    assert_waiting(h, "alternate_reviewer", [])
    assert not read(h, "alternate_reviewer", id=row["id"])["item"]["can_decide"]
    payload = {"decision": "approve", "reason": "Reviewed this request"}
    refused = h.command("alternate_reviewer", "request.decide", payload, row["id"], row["revision"], status=403)
    assert refused["error"]["code"] == "SELF_APPROVAL"
    permission = h.command("admin", "self_approval.grant", {"identity_id": reviewer, "actions": ["request.decide"],
        "starts_on": "2026-01-01", "ends_on": "2027-01-01", "reason": "Explicit request permission"})["permission"]
    assert_waiting(h, "alternate_reviewer", [row])
    refused = h.command("alternate_reviewer", "request.decide", {"decision": "approve"},
                        row["id"], row["revision"], status=422)
    assert refused["error"]["code"] == "SELF_APPROVAL_REASON"
    decided = h.command("alternate_reviewer", "request.decide", payload, row["id"], row["revision"])["record"]
    assert decided["status"] == "approved" and decided["created_by"] == h.seed["identities"][entered_by]
    assert decided["decided_by"] == reviewer
    event = next(e for e in h.get("auditor", "/api/audit?self_approved=true")["events"] if e["action"] == "request.decide")
    assert event["detail"]["self_approved"] == [{"action": "request.decide", "permission_id": permission["id"]}]
    assert_waiting(h, "alternate_reviewer", [])


def test_on_behalf_batch_needs_no_self_approval_or_note(database):
    h = database
    reviewer = identity(h, "request_reviewer", ["coordinator", "approver"], person=h.seed["people"][0])
    rows = [request_row(h, "request_reviewer", person, kind="meeting") for person in h.seed["people"][1:3]]
    assert_waiting(h, "request_reviewer", rows)
    result = decide_many(h, rows, role="request_reviewer", reason="")
    assert result["count"] == 2
    assert all(row["status"] == "approved" and row["created_by"] == row["decided_by"] == reviewer
               for row in result["records"])
    assert not h.get("auditor", "/api/audit?self_approved=true")["events"]
    assert_waiting(h, "request_reviewer", [])


@pytest.mark.parametrize("linked", [False, True])
def test_person_fact_entered_for_another_subject_is_independent(database, linked):
    h = database
    reviewer = identity(h, "fact_reviewer", ["coordinator", "approver"],
                        person=h.seed["people"][0] if linked else None)
    fact = h.command("fact_reviewer", "person.workload.propose", workload(h, person_id=h.seed["people"][1]))["fact"]
    assert_eligibility(h, fact, "fact_reviewer", True)
    waiting = read(h, "fact_reviewer", for_me=1)
    assert {item["id"] for item in waiting["items"]} == {fact["id"]}
    assert waiting["counts"]["to_decide"] == 1
    activated = h.command("fact_reviewer", "person.workload.activate", {}, fact["id"], fact["revision"])["fact"]
    assert activated["status"] == "active" and activated["proposed_by"] == activated["approved_by"] == reviewer
    assert not h.get("auditor", "/api/audit?self_approved=true")["events"]
    assert read(h, "fact_reviewer", for_me=1)["counts"]["to_decide"] == 0


def test_swap_other_subject_requires_self_approval_and_stays_out_of_waiting(swap):
    h, _, cohort, types, people, rotations = swap
    person = people[1]
    peer = h.configure("rotations", {"person_id": person, "cohort_id": cohort["id"],
        "rotation_type_id": types[1]["id"], "starts_on": "2026-12-01", "ends_on": "2026-12-15"}, role="coordinator")["record"]
    reviewer = identity(h, "swap_reviewer", ["coordinator", "approver"], person=person)
    row = propose(h, [rotations[0], peer], role="swap_reviewer")["record"]
    assert row["person_id"] != person and row["details"]["swap"]["person_id"] == person
    assert_waiting(h, "swap_reviewer", [])
    assert not read(h, "swap_reviewer", id=row["id"])["item"]["can_decide"]
    payload = {"decision": "reject", "reason": "Reviewed both sides of the exchange"}
    assert h.command("swap_reviewer", "request.decide", payload, row["id"], row["revision"],
                     status=403)["error"]["code"] == "SELF_APPROVAL"
    permission = h.command("admin", "self_approval.grant", {"identity_id": reviewer, "actions": ["request.decide"],
        "starts_on": "2026-01-01", "ends_on": "2027-01-01", "reason": "Explicit exchange permission"})["permission"]
    assert_waiting(h, "swap_reviewer", [row])
    assert h.command("swap_reviewer", "request.decide", payload, row["id"], row["revision"])["record"]["status"] == "rejected"
    event = next(e for e in h.get("auditor", "/api/audit?self_approved=true")["events"] if e["action"] == "request.decide")
    assert event["detail"]["self_approved"] == [{"action": "request.decide", "permission_id": permission["id"]}]
    assert_waiting(h, "swap_reviewer", [])


def test_overtime_entered_for_another_subject_is_independent(database):
    h = database
    reviewer = identity(h, "offer_reviewer", ["coordinator"], person=h.seed["people"][1])
    offer_rule(h)
    row = offer(h, "offer_reviewer")["record"]
    assert_waiting(h, "offer_reviewer", [row])
    decided = h.command("offer_reviewer", "request.decide", {"decision": "approve"},
                        row["id"], row["revision"])["record"]
    assert decided["status"] == "approved" and decided["created_by"] == decided["decided_by"] == reviewer
    assert not h.get("auditor", "/api/audit?self_approved=true")["events"]
    assert_waiting(h, "offer_reviewer", [])


def test_calendar_fact_without_a_person_is_independent_of_its_proposer(database):
    h = database
    reviewer = identity(h, "calendar_reviewer", ["coordinator", "approver"], person=h.seed["people"][0])
    entry = calendar_entry(h)
    fact = h.command("calendar_reviewer", "calendar.protection.propose", {
        "kind": "protected_time", "catalogue_id": "protected_time", "calendar_entry_id": entry["id"],
        "line": {"id": str(uuid.uuid4()), "starts_on": entry["starts_on"], "ends_on": entry["ends_on"],
                 "numbers": {"apply": {"value": None, "level": "mandatory"}}},
        "reason": "Synthetic calendar protection"})["fact"]
    assert fact["person_id"] is None
    assert_eligibility(h, fact, "calendar_reviewer", True)
    assert {row["id"] for row in read(h, "calendar_reviewer", for_me=1)["items"]} == {fact["id"]}
    activated = h.command("calendar_reviewer", "calendar.protection.activate", {}, fact["id"], fact["revision"])["fact"]
    assert activated["status"] == "active" and activated["proposed_by"] == activated["approved_by"] == reviewer
    assert not h.get("auditor", "/api/audit?self_approved=true")["events"]
