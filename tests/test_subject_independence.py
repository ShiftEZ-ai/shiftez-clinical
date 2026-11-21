"""Decision independence follows the subject, while authorship remains in the audit trail."""
import uuid
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import String, literal, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services import bulk as bulk_service, independence
from .governance_support import identity
from .test_request_decide_many import decide as decide_many, request_row
from .test_requests_for_me import read
from .test_rc17_rotation_swap import propose, swap
from .test_scheduling_fact_self_approval import assert_eligibility
from .test_scheduling_fact_approvals import calendar_entry
from .test_overtime_volunteer import offer, offer_rule
from .test_scheduling_facts_api import workload
from .test_availability_patterns import pattern
from .test_exception_lifecycle import row as exception_row


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
    assert event["detail"]["entered_on_behalf_by"] == [reviewer]
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
    events = h.get("auditor", "/api/audit")["events"]
    decisions = [e for e in events if e["action"] == "request.decide"]
    assert {e["target_id"] for e in decisions} == {row["id"] for row in rows}
    assert all(e["detail"]["entered_on_behalf_by"] == [reviewer] for e in decisions)
    event = next(e for e in events if e["action"] == "request.decide_many")
    assert "entered_on_behalf_by" not in event["detail"]
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
    event = next(e for e in h.get("auditor", "/api/audit")["events"] if e["action"] == "person.workload.activate")
    assert event["detail"]["entered_on_behalf_by"] == [reviewer]
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
    assert "entered_on_behalf_by" not in event["detail"]
    assert_waiting(h, "swap_reviewer", [])


@pytest.mark.parametrize("enterer_is_party", [True, False], ids=["swap-party", "outside-coordinator"])
def test_rotation_swap_approval_marks_only_an_outside_enterer(swap, enterer_is_party):
    h, _, cohort, types, people, rotations = swap
    peer = h.configure("rotations", {"person_id": people[1], "cohort_id": cohort["id"],
        "rotation_type_id": types[1]["id"], "starts_on": "2026-12-01", "ends_on": "2026-12-15"}, role="coordinator")["record"]
    enterer = identity(h, "swap_enterer", ["coordinator"],
                       person=people[1] if enterer_is_party else h.seed["people"][2])
    row = propose(h, [rotations[0], peer], role="swap_enterer")["record"]
    assert (row["person_id"], row["details"]["swap"]["person_id"]) == tuple(people)
    approved = h.command("approver", "request.decide", {"decision": "approve", "reason": "Reviewed the exchange"},
                         row["id"], row["revision"])["record"]
    assert approved["status"] == "approved" and approved["decided_by"] == h.seed["identities"]["approver"]
    event = next(e for e in h.get("auditor", "/api/audit")["events"]
                 if e["action"] == "request.decide" and e["target_id"] == row["id"])
    assert "self_approved" not in event["detail"]
    if enterer_is_party:
        assert "entered_on_behalf_by" not in event["detail"]
    else:
        assert event["detail"]["entered_on_behalf_by"] == [enterer]


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


def calendar_protection(h):
    entry = calendar_entry(h)
    return h.command("calendar_reviewer", "calendar.protection.propose", {
        "kind": "protected_time", "catalogue_id": "protected_time", "calendar_entry_id": entry["id"],
        "line": {"id": str(uuid.uuid4()), "starts_on": entry["starts_on"], "ends_on": entry["ends_on"],
                 "numbers": {"apply": {"value": None, "level": "mandatory"}}},
        "reason": "Synthetic calendar protection"})["fact"]


def activate_calendar(h, role, fact, *, bulk=False, reason="", status=200):
    if bulk:
        return h.command(role, "bulk.apply", {"reason": reason, "items": [{
            "item_kind": "calendar.protection.activate", "target_id": fact["id"],
            "expected_revision": fact["revision"], "payload": {}}]}, status=status)
    return h.command(role, "calendar.protection.activate", {"reason": reason},
                     fact["id"], fact["revision"], status=status)


@pytest.mark.parametrize("identity_mode", ["unlinked", "linked", "alias"])
@pytest.mark.parametrize("bulk", [False, True])
def test_no_subject_calendar_protection_refuses_proposer_without_audited_permission(database, identity_mode, bulk):
    h = database
    person = h.seed["people"][0] if identity_mode != "unlinked" else None
    proposer = identity(h, "calendar_reviewer", ["coordinator", "approver"], person=person)
    role = "alternate_reviewer" if identity_mode == "alias" else "calendar_reviewer"
    reviewer = identity(h, role, ["coordinator", "approver"], person=person) if role != "calendar_reviewer" else proposer
    fact = calendar_protection(h)
    assert fact["person_id"] is None
    assert_eligibility(h, fact, role, False)
    assert read(h, role, for_me=1)["items"] == []
    refused = activate_calendar(h, role, fact, bulk=bulk, reason="Review the calendar protection", status=403)
    assert refused["error"]["code"] == "SELF_APPROVAL"
    assert read(h, role, id=fact["id"])["fact"] == fact
    assert not h.get("auditor", "/api/audit?self_approved=true")["events"]
    permission = h.command("admin", "self_approval.grant", {"identity_id": reviewer, "actions": ["rule.activate"],
        "starts_on": "2026-01-01", "ends_on": "2027-01-01", "reason": "Explicit calendar approval permission"})["permission"]
    assert_eligibility(h, fact, role, True)
    assert {row["id"] for row in read(h, role, for_me=1)["items"]} == {fact["id"]}
    refused = activate_calendar(h, role, fact, bulk=bulk, reason="Routine update", status=422)
    assert refused["error"]["code"] == "SELF_APPROVAL_REASON"
    activate_calendar(h, role, fact, bulk=bulk, reason="Reviewed with independent calendar cover")
    activated = read(h, role, id=fact["id"])["fact"]
    assert activated["status"] == "active" and activated["proposed_by"] == proposer and activated["approved_by"] == reviewer
    event = next(e for e in h.get("auditor", "/api/audit?self_approved=true")["events"]
                 if e["action"] == "calendar.protection.activate")
    assert event["detail"]["self_approved"] == [{"action": "rule.activate", "permission_id": permission["id"]}]
    assert "entered_on_behalf_by" not in event["detail"]


@pytest.mark.parametrize("bulk", [False, True])
def test_no_subject_calendar_protection_allows_a_different_approver(database, bulk):
    h = database
    proposer = identity(h, "calendar_reviewer", ["coordinator", "approver"], person=h.seed["people"][0])
    fact = calendar_protection(h)
    assert_eligibility(h, fact, "approver", True)
    activate_calendar(h, "approver", fact, bulk=bulk, reason="Independent calendar review")
    activated = read(h, "approver", id=fact["id"])["fact"]
    assert activated["status"] == "active" and activated["proposed_by"] == proposer
    assert activated["approved_by"] == h.seed["identities"]["approver"]
    assert not h.get("auditor", "/api/audit?self_approved=true")["events"]


def personal_entry(h, source, entered_by, person):
    if source == "pattern":
        # Retained proposals may have been entered by a coordinator or a login linked to another person.
        with transaction(h.app.state.engine, write=True) as db:
            row = m.AvailabilityPattern(department_id=h.seed["department_id"],
                created_by=h.seed["identities"][entered_by], status="proposed", **pattern(person, starts_on=date(2026, 9, 1)))
            db.add(row)
            db.flush()
            return record(row)
    policy = h.policy({"kind": "max_duties", "maximum": 3}, exception=True)
    return h.command(entered_by, "exception.create", {"policy_id": policy["id"], "person_id": person,
        "starts_on": "2026-10-04", "ends_on": "2026-10-11", "reason": "Synthetic person exception"})["record"]


def assert_personal_eligibility(h, source, row, allowed):
    if source == "pattern":
        item = read(h, "person_reviewer", id=row["id"])["item"]
        assert item["can_decide"] is allowed
        page = read(h, "person_reviewer", for_me=1)
        assert {item["id"] for item in page["items"]} == ({row["id"]} if allowed else set())
        assert page["counts"]["to_decide"] == int(allowed)
    else:
        assert exception_row(h, "person_reviewer", row)["can_approve"] is allowed
    attention = {item["key"]: item for item in h.get("person_reviewer", "/api/home")["attention"]}
    key = "patterns" if source == "pattern" else "exceptions"
    assert attention.get(key, {}).get("count", 0) == int(allowed)


@pytest.mark.parametrize("source", ["pattern", "exception"])
@pytest.mark.parametrize("linked", [False, True])
def test_on_behalf_person_entry_allows_enterer_and_marks_audit(database, source, linked):
    h = database
    reviewer = identity(h, "person_reviewer", ["coordinator", "approver"],
                        person=h.seed["people"][1] if linked else None)
    row = personal_entry(h, source, "person_reviewer", h.seed["people"][0])
    assert_personal_eligibility(h, source, row, True)
    command = "availability_pattern.decide" if source == "pattern" else "exception.approve"
    payload = {"reason": "Independent person review", **({"decision": "approve"} if source == "pattern" else {})}
    result = h.command("person_reviewer", command, payload, row["id"], row["revision"])
    decided = result["pattern" if source == "pattern" else "record"]
    assert decided["status"] == ("active" if source == "pattern" else "approved")
    assert decided["created_by"] == reviewer == decided["decided_by" if source == "pattern" else "approved_by"]
    event = next(e for e in h.get("auditor", "/api/audit")["events"] if e["action"] == command)
    assert event["detail"]["entered_on_behalf_by"] == [reviewer]
    assert "self_approved" not in event["detail"]


@pytest.mark.parametrize("source", ["pattern", "exception"])
@pytest.mark.parametrize("entered_by", ["coordinator", "subject_alias"])
def test_own_person_entry_requires_permission_reason_and_self_approved_mark(database, source, entered_by):
    h = database
    person = h.seed["people"][0]
    reviewer = identity(h, "person_reviewer", ["coordinator", "approver"], person=person)
    identity(h, "subject_alias", ["coordinator", "approver"], person=person)
    row = personal_entry(h, source, entered_by, person)
    assert_personal_eligibility(h, source, row, False)
    command = "availability_pattern.decide" if source == "pattern" else "exception.approve"
    payload = {"reason": "Reviewed my own matter with independent cover",
               **({"decision": "approve"} if source == "pattern" else {})}
    assert h.command("person_reviewer", command, payload, row["id"], row["revision"],
                     status=403)["error"]["code"] == "SELF_APPROVAL"
    action = "request.decide" if source == "pattern" else "exception.approve"
    permission = h.command("admin", "self_approval.grant", {"identity_id": reviewer, "actions": [action],
        "starts_on": "2026-01-01", "ends_on": "2027-01-01", "reason": "Explicit person decision permission"})["permission"]
    assert_personal_eligibility(h, source, row, True)
    assert h.command("person_reviewer", command, {**payload, "reason": "Routine update"}, row["id"], row["revision"],
                     status=422)["error"]["code"] == "SELF_APPROVAL_REASON"
    result = h.command("person_reviewer", command, payload, row["id"], row["revision"])
    decided = result["pattern" if source == "pattern" else "record"]
    assert decided["status"] == ("active" if source == "pattern" else "approved")
    event = next(e for e in h.get("auditor", "/api/audit?self_approved=true")["events"] if e["action"] == command)
    assert event["detail"]["self_approved"] == [{"action": action, "permission_id": permission["id"]}]
    assert event["detail"].get("entered_on_behalf_by", []) == ([h.seed["identities"][entered_by]] if entered_by == "coordinator" else [])


def test_null_swap_details_keep_request_subject_independence(database):
    h = database
    reviewer = identity(h, "request_reviewer", ["coordinator", "approver"], person=h.seed["people"][1])
    row = request_row(h, "request_reviewer", h.seed["people"][0], kind="meeting")
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.RequestRecord, row["id"]).details = {"swap": None}
    assert_waiting(h, "request_reviewer", [row])
    decided = h.command("request_reviewer", "request.decide", {"decision": "approve"}, row["id"], row["revision"])["record"]
    assert decided["status"] == "approved" and decided["decided_by"] == reviewer


def test_direct_on_behalf_pattern_records_enterer_in_audit(database):
    h = database
    reviewer = identity(h, "pattern_reviewer", ["coordinator"], person=h.seed["people"][1])
    row = h.command("pattern_reviewer", "availability_pattern.propose", pattern(h.seed["people"][0]))["pattern"]
    assert row["status"] == "active" and row["created_by"] == row["decided_by"] == reviewer
    event = next(e for e in h.get("auditor", "/api/audit")["events"] if e["action"] == "availability_pattern.propose")
    assert event["detail"]["entered_on_behalf_by"] == [reviewer] and "self_approved" not in event["detail"]


def test_shared_subject_independence_matches_python_and_sql(database):
    h = database
    first, second = h.seed["people"][:2]
    people = {"reviewer": first, "alias": first, "other": second, "unlinked": None}
    actors = {role: SimpleNamespace(identity_id=identity(h, role, ["coordinator", "approver"], person=person),
                                  person_id=person) for role, person in people.items()}
    cases = [
        ((first,), "reviewer", "reviewer", False),
        ((second,), "reviewer", "reviewer", True),
        ((first,), "reviewer", "alias", False),
        ((None,), "reviewer", "alias", False),
        ((), "reviewer", "other", True),
        ((None,), "unlinked", "unlinked", False),
        ((first,), "unlinked", "unlinked", True),
        ((first, second), "reviewer", "other", False),
        ((None, second), "reviewer", "other", False),
        ((None,), "reviewer", "unlinked", True),
    ]
    with transaction(h.app.state.engine) as db:
        for subjects, entered_by, reviewer, allowed in cases:
            actor, enterer = actors[reviewer], actors[entered_by].identity_id
            assert independence.subject_independent(db, actor, subjects, enterer) is allowed
            query = independence.subject_independent(db, actor, tuple(literal(person, type_=String) for person in subjects), literal(enterer), sql=True)
            assert db.scalar(select(query)) is allowed
        assert "entered_on_behalf_by" not in db.info


@pytest.mark.parametrize("source", ["request", "pattern"])
def test_request_batch_decisions_keep_on_behalf_and_self_approval_audits_separate(database, source):
    h = database
    reviewer = identity(h, "request_reviewer", ["coordinator", "approver"], person=h.seed["people"][0])
    rows = [{**(request_row(h, "request_reviewer", person, kind="meeting") if source == "request" else
                personal_entry(h, source, "request_reviewer", person)), "source": source}
            for person in (h.seed["people"][1], h.seed["people"][0])]
    permission = h.command("admin", "self_approval.grant", {"identity_id": reviewer, "actions": ["request.decide"],
        "starts_on": "2026-01-01", "ends_on": "2027-01-01", "reason": "Explicit personal decision permission"})["permission"]
    result = decide_many(h, rows, role="request_reviewer", reason="Reviewed both matters with independent cover")
    assert result["count"] == 2 and all(row["stage"] == "approved" for row in result["records"])
    events = h.get("auditor", "/api/audit")["events"]
    action = "request.decide" if source == "request" else "availability_pattern.decide"
    decisions = {e["target_id"]: e for e in events if e["action"] == action}
    assert set(decisions) == {row["id"] for row in rows}
    assert decisions[rows[0]["id"]]["detail"]["entered_on_behalf_by"] == [reviewer]
    assert "self_approved" not in decisions[rows[0]["id"]]["detail"]
    assert decisions[rows[1]["id"]]["detail"]["self_approved"] == [
        {"action": "request.decide", "permission_id": permission["id"]}]
    assert "entered_on_behalf_by" not in decisions[rows[1]["id"]]["detail"]
    parent = next(e for e in events if e["action"] == "request.decide_many")
    assert "entered_on_behalf_by" not in parent["detail"] and "self_approved" not in parent["detail"]


def test_bulk_person_fact_decisions_keep_on_behalf_and_self_approval_audits_separate(database):
    h = database
    reviewer = identity(h, "fact_reviewer", ["coordinator", "approver"], person=h.seed["people"][0])
    rows = [h.command("fact_reviewer", "person.workload.propose", workload(h, person_id=person))["fact"]
            for person in (h.seed["people"][1], h.seed["people"][0])]
    permission = h.command("admin", "self_approval.grant", {"identity_id": reviewer, "actions": ["rule.activate"],
        "starts_on": "2026-01-01", "ends_on": "2027-01-01", "reason": "Explicit personal fact permission"})["permission"]
    result = h.command("fact_reviewer", "bulk.apply", {"reason": "Reviewed both facts with independent cover", "items": [
        {"item_kind": "person.workload.activate", "target_id": row["id"], "expected_revision": row["revision"], "payload": {}}
        for row in rows]})
    assert result["count"] == 2
    assert all(read(h, "fact_reviewer", id=row["id"])["fact"]["status"] == "active" for row in rows)
    events = h.get("auditor", "/api/audit")["events"]
    decisions = {event["target_id"]: event for event in events if event["action"] == "person.workload.activate"}
    assert decisions[rows[0]["id"]]["detail"]["entered_on_behalf_by"] == [reviewer]
    assert "self_approved" not in decisions[rows[0]["id"]]["detail"]
    assert decisions[rows[1]["id"]]["detail"]["self_approved"] == [{"action": "rule.activate", "permission_id": permission["id"]}]
    assert "entered_on_behalf_by" not in decisions[rows[1]["id"]]["detail"]
    parent = next(event for event in events if event["action"] == "bulk.apply")
    assert "entered_on_behalf_by" not in parent["detail"] and "self_approved" not in parent["detail"]


@pytest.mark.parametrize("existing", [False, True])
def test_failed_bulk_item_restores_approval_audit_metadata(database, monkeypatch, existing):
    h = database
    actor = SimpleNamespace(identity_id=h.seed["identities"]["coordinator"], person_id=None)

    def fail(db, *args):
        independence.ensure_subject(db, actor, "rule.activate", (h.seed["people"][0],), actor.identity_id,
                                    "Independent fact review")
        db.info.setdefault("self_approved", []).append({"action": "rule.activate", "permission_id": "synthetic"})
        raise ValueError("Synthetic later item refusal")

    monkeypatch.setattr(bulk_service, "_dispatch_item", fail)
    with transaction(h.app.state.engine) as db:
        previous = {"self_approved": [{"action": "request.decide", "permission_id": "previous"}],
                    "entered_on_behalf_by": {"previous"}} if existing else {}
        db.info.update(deepcopy(previous))
        with pytest.raises(ValueError, match="Synthetic later item refusal"):
            bulk_service._run_item(db, actor, {}, {}, None, "synthetic")
        assert {key: db.info[key] for key in ("self_approved", "entered_on_behalf_by") if key in db.info} == previous
