"""Bulk request decisions preserve single-record authority and commit or undo one complete batch."""
from datetime import datetime, timedelta, timezone
import json
import time
import uuid

import pytest
from sqlalchemy import func, select

from apps.api import auth, models as m
from apps.api.db import transaction
from packages.contracts.schemas import ROLE_NAMES
from .clock_support import freeze_clock
from .governance_support import identity
from .test_overtime_volunteer import offer, offer_rule


REASON = "Cover arranged after independent review"


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def database(request):
    return request.getfixturevalue(request.param)


def request_row(h, role="staff", person=None, kind="leave"):
    # These fixtures are distinct requests; exact duplicates now fail inside the serialized create transaction.
    number = getattr(h, '_batch_request_number', 0)
    h._batch_request_number = number + 1
    start = datetime(2026, 10, 4, 9, tzinfo=timezone(timedelta(hours=3))) + timedelta(hours=2 * number)
    return {**h.command(role, "request.create", {
        "person_id": person or h.seed["people"][0], "kind": kind,
        "start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(),
        "private_reason": "Fictional confidential request",
    })["record"], "source": "request"}


def pattern_row(h):
    return {**h.command("staff", "availability_pattern.propose", {
        "person_id": h.seed["people"][0], "weekdays": [1], "start_time": "13:00",
        "end_time": "17:00", "starts_on": "2026-10-01",
    })["pattern"], "source": "pattern"}


def item(row):
    return {"id": row["id"], "expected_revision": row["revision"], "source": row["source"]}


def decide(h, rows, decision="approve", role="approver", status=200, key=None, reason=REASON):
    return h.command(role, "request.decide_many", {
        "items": [item(row) for row in rows], "decision": decision, "reason": reason,
    }, status=status, key=key)


def state(h):
    """Include effects a rollback must remove, beyond the visible request statuses."""
    with transaction(h.app.state.engine) as db:
        org = db.get(m.Organisation, h.seed["organisation_id"])
        return {
            "requests": sorted((r.id, r.status, r.revision, r.decided_by, r.decision_reason, r.workspace_id)
                               for r in db.scalars(select(m.RequestRecord))),
            "patterns": sorted((r.id, r.status, r.revision, r.decided_by)
                               for r in db.scalars(select(m.AvailabilityPattern))),
            "availability": sorted((r.id, r.request_id, r.retracted, r.revision)
                                   for r in db.scalars(select(m.Availability))),
            "inputs": (org.input_revision, org.availability_revision),
            "counts": [db.scalar(select(func.count()).select_from(cls).where(
                           cls.action != "command.refused")) if cls is m.AuditEvent
                       else db.scalar(select(func.count()).select_from(cls))
                       for cls in (m.AuditEvent, m.Outbox, m.CommandReceipt)],
        }


@pytest.mark.parametrize("role", ROLE_NAMES)
@pytest.mark.parametrize("source", ["request", "pattern"])
def test_each_source_keeps_its_existing_role_permissions(h, role, source):
    if role not in h.seed["identities"]:
        identity(h, role, [role])
    row = request_row(h) if source == "request" else pattern_row(h)
    permitted = role in ({"admin", "approver"} if source == "request" else {"admin", "coordinator"})
    h.client(role)
    before = state(h)
    reply = decide(h, [row], role=role, status=200 if permitted else 403)
    if permitted:
        assert reply["count"] == 1 and reply["records"][0]["stage"] == "approved"
    else:
        assert reply["error"]["code"] == "FORBIDDEN"
        assert state(h) == before


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_single_logic_compatibility_and_one_undo(database, decision):
    h = database
    single = request_row(h)
    single = h.command("approver", "request.decide", {"decision": decision, "reason": REASON},
                       single["id"], single["revision"])["record"]
    rows = [request_row(h), request_row(h, kind="meeting")]
    reply = decide(h, rows, decision)
    assert reply["count"] == 2
    assert [r["id"] for r in reply["records"]] == [r["id"] for r in rows]
    for row in reply["records"]:
        assert row["status"] == single["status"] == ("approved" if decision == "approve" else "rejected")
        assert row["decided_by"] == single["decided_by"] == h.seed["identities"]["approver"]
        assert row["decision_reason"] == single["decision_reason"] == REASON
        assert row["revision"] == single["revision"] == 2
        assert row["stage"] == ("approved" if decision == "approve" else "declined")
        assert "private_reason" not in row
    undone = h.command("approver", "request.undo", {"batch_id": reply["batch_id"], "reason": "Undo reviewed batch"})
    assert undone["count"] == 2
    assert {r["id"] for r in undone["records"]} == {r["id"] for r in rows}
    assert all(r["status"] == "submitted" and r["stage"] == "waiting" and r["revision"] == 3
               and r["decided_by"] is None and r["decision_reason"] is None for r in undone["records"])
    with transaction(h.app.state.engine) as db:
        absences = list(db.scalars(select(m.Availability).where(m.Availability.request_id.in_([r["id"] for r in rows]))))
        assert len(absences) == (1 if decision == "approve" else 0)
        assert all(a.retracted for a in absences)
        assert db.get(m.RequestRecord, single["id"]).status == single["status"]
    before = state(h)
    h.command("approver", "request.undo", {"batch_id": reply["batch_id"], "reason": "Already undone"}, status=409)
    assert state(h) == before


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_mixed_sources_reply_audit_and_idempotency(database, decision):
    h = database
    rows = [request_row(h), pattern_row(h)]
    key = str(uuid.uuid4())
    inputs_before = state(h)["inputs"]
    reply = decide(h, rows, decision, "admin", key=key)
    assert reply["count"] == 2 and reply["batch_id"] and reply["receipt_id"]
    assert [r["source"] for r in reply["records"]] == ["request", "pattern"]
    expected_inputs = tuple(v + (decision == "approve") for v in inputs_before)
    assert (reply["input_revision"], reply["availability_revision"]) == expected_inputs == state(h)["inputs"]
    assert reply["counts"]["to_decide"] == h.get("admin", "/api/requests/all")["counts"]["to_decide"]
    for patch in reply["records"]:
        current = h.get("admin", "/api/requests/all?id=" + patch["id"])["item"]
        for field in ("id", "source", "stage", "status", "revision", "decided_by"):
            assert patch[field] == current[field]
        if patch["source"] == "request" and decision == "approve":
            for field in ("record_id", "record_revision", "record_retracted", "counts_as_unavailable",
                          "leave_type_id", "leave_type_name"):
                assert patch[field] == current[field]
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.target_id.in_([r["id"] for r in rows]))))
        decisions = [e for e in events if e.action in ("request.decide", "availability_pattern.decide")]
        assert len(decisions) == 2
        assert {e.action for e in decisions} == {"request.decide", "availability_pattern.decide"}
        assert all(e.reason == REASON and e.actor_id == h.seed["identities"]["admin"]
                   and e.detail["batch_id"] == reply["batch_id"] and e.detail["receipt_id"] == reply["receipt_id"]
                   for e in decisions)
        assert all("Fictional confidential request" not in json.dumps(e.detail) for e in events)
        generic = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "request.decide_many")).one()
        assert generic.detail["receipt_id"] == reply["receipt_id"]
        assert generic.detail["batch_id"] == reply["batch_id"] and generic.detail["count"] == 2
    before = state(h)
    assert decide(h, rows, decision, "admin", key=key) == reply
    assert state(h) == before
    h.command("admin", "request.decide_many", {"items": [item(r) for r in rows], "decision": decision,
              "reason": "Different content"}, key=key, status=409)
    assert state(h) == before
    undone = h.command("admin", "request.undo", {"batch_id": reply["batch_id"], "reason": "Undo mixed batch"})
    assert (undone["input_revision"], undone["availability_revision"]) == tuple(v + (decision == "approve") for v in expected_inputs)
    assert undone["counts"]["to_decide"] == h.get("admin", "/api/requests/all")["counts"]["to_decide"]
    assert [r["source"] for r in undone["records"]] == ["request", "pattern"]
    assert [r["status"] for r in undone["records"]] == ["submitted", "proposed"]
    assert all(r["stage"] == "waiting" and r["decided_by"] is None for r in undone["records"])
    with transaction(h.app.state.engine) as db:
        generic = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "request.undo",
                                                       m.AuditEvent.target_id.is_(None))).one()
        assert generic.detail["receipt_id"] == undone["receipt_id"]
        assert generic.detail["batch_id"] == reply["batch_id"] and generic.detail["count"] == 2


def test_coordinator_overtime_batch_receipt_and_replay(database):
    h = database
    identity(h, "overtime_coordinator", ["coordinator"])
    offer_rule(h)
    rows = [{**offer(h, dates=[day])["record"], "source": "request"}
            for day in ("2026-10-05", "2026-10-06")]
    key = str(uuid.uuid4())
    reply = decide(h, rows, role="overtime_coordinator", key=key)
    assert reply["count"] == 2 and all(row["status"] == "approved" for row in reply["records"])
    before = state(h)
    assert h.get("overtime_coordinator", "/api/commands/" + key) == {"status": "committed", "result": reply}
    assert decide(h, rows, role="overtime_coordinator", key=key) == reply
    assert state(h) == before


@pytest.mark.parametrize("revoked_role", ["approver", "coordinator"])
def test_mixed_request_kinds_need_each_decider_for_decision_receipt_and_replay(database, revoked_role):
    h = database
    reviewer = identity(h, "mixed_reviewer", ["coordinator", "approver"])
    identity(h, "overtime_coordinator", ["coordinator"])
    offer_rule(h)
    rows = [{**offer(h)["record"], "source": "request"}, request_row(h)]
    h.client("overtime_coordinator")
    before = state(h)
    assert decide(h, rows, role="overtime_coordinator", status=403)["error"]["code"] == "FORBIDDEN"
    assert state(h) == before
    key = str(uuid.uuid4())
    reply = decide(h, rows, role="mixed_reviewer", key=key)
    assert h.get("mixed_reviewer", "/api/commands/" + key)["result"] == reply
    assert decide(h, rows, role="mixed_reviewer", key=key) == reply
    with transaction(h.app.state.engine, write=True) as db:
        binding = db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == reviewer,
                                                       m.RoleBinding.role == revoked_role)).one()
        binding.revoked = True
    before = state(h)
    assert h.get("mixed_reviewer", "/api/commands/" + key, status=403)["error"]["code"] == "FORBIDDEN"
    assert decide(h, rows, role="mixed_reviewer", key=key, status=403)["error"]["code"] == "FORBIDDEN"
    assert state(h) == before


@pytest.mark.parametrize("failure", ["withdrawn", "stale", "foreign"])
def test_first_ineligible_request_rolls_back_the_entire_batch(database, failure):
    h = database
    rows = [request_row(h) for _ in range(3)]
    if failure == "withdrawn":
        rows[1] = {**h.command("staff", "request.decide", {"decision": "withdraw", "reason": "Plans changed"},
                                rows[1]["id"], rows[1]["revision"])["record"], "source": "request"}
    elif failure == "stale":
        rows[1] = {**rows[1], "revision": rows[1]["revision"] + 1}
    else:
        with transaction(h.app.state.engine, write=True) as db:
            department = m.Department(organisation_id=h.seed["organisation_id"], name="Other test department")
            db.add(department)
            db.flush()
            db.get(m.RequestRecord, rows[1]["id"]).department_id = department.id
    h.client("approver")
    before = state(h)
    reply = decide(h, rows, status=404 if failure == "foreign" else 409)
    assert reply["error"]["code"] == {"withdrawn": "WORKFLOW_STATE", "stale": "STALE_REVISION", "foreign": "NOT_FOUND"}[failure]
    assert "0 of 3 done" in reply["error"]["message"]
    assert reply["error"]["details"]["completed"] == 0
    assert reply["error"]["details"]["failed_index"] == 1
    assert reply["error"]["details"]["id"] == rows[1]["id"]
    assert state(h) == before


@pytest.mark.parametrize("role,first_source", [("approver", "request"), ("coordinator", "pattern")])
def test_permissions_are_checked_for_each_item_and_rollback_the_prefix(h, role, first_source):
    rows = [request_row(h), pattern_row(h)]
    if first_source == "pattern":
        rows.reverse()
    h.client(role)
    before = state(h)
    assert decide(h, rows, role=role, status=403)["error"]["code"] == "FORBIDDEN"
    assert state(h) == before


@pytest.mark.parametrize("own_by", ["person", "submitter"])
def test_an_own_request_refuses_the_whole_batch(h, own_by):
    identity(h, "own_reviewer", ["coordinator", "approver"],
             h.seed["people"][0] if own_by == "person" else None)
    other = request_row(h, "coordinator", h.seed["people"][1])
    own = request_row(h) if own_by == "person" else request_row(h, "own_reviewer", h.seed["people"][1])
    h.client("own_reviewer")
    before = state(h)
    assert decide(h, [other, own], role="own_reviewer", status=403)["error"]["code"] == "SELF_APPROVAL"
    assert state(h) == before


def test_existing_self_approval_permission_still_requires_reason_and_is_audited(h):
    reviewer = identity(h, "own_reviewer", ["approver"], h.seed["people"][0])
    own = request_row(h)
    assert h.get("own_reviewer", "/api/requests/all?id=" + own["id"])["item"]["can_decide"] is False
    permission = h.command("admin", "self_approval.grant", {"identity_id": reviewer, "actions": ["request.decide"],
        "starts_on": "2026-01-01", "ends_on": "2027-01-01", "reason": "Explicit synthetic self-approval permission"})["permission"]
    h.client("own_reviewer")
    assert h.get("own_reviewer", "/api/requests/all?id=" + own["id"])["item"]["can_decide"] is True
    before = state(h)
    refused = decide(h, [own], role="own_reviewer", reason="Routine update", status=422)
    assert refused["error"]["code"] == "SELF_APPROVAL_REASON" and state(h) == before
    reply = decide(h, [own], role="own_reviewer", reason="Reviewed with independent cover by phone")
    assert h.get("own_reviewer", "/api/requests/all?id=" + own["id"])["item"]["can_decide"] is False
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action.in_(("request.decide", "request.decide_many")))))
        assert any({"action": "request.decide", "permission_id": permission["id"]} in e.detail.get("self_approved", [])
                   for e in events if e.detail.get("receipt_id") == reply["receipt_id"])


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("source", ["request", "pattern"])
def test_list_eligibility_preserves_role_and_ownership_for_bulk_decisions(database, source, request):
    h = request.getfixturevalue(database)
    own_person = h.seed["people"][1]
    identity(h, "reviewer", ["coordinator", "approver"], person=own_person)
    identity(h, "same_person", ["coordinator"], person=own_person)
    if source == "request":
        other = request_row(h)
        own_target = request_row(h, "coordinator", own_person)
        own_submission = request_row(h, "reviewer")
        alternate_submission = request_row(h, "same_person")
        rows = [other, own_target, own_submission, alternate_submission]
        eligible_ids = {other["id"]}
    else:
        other = pattern_row(h)
        # Coordinators' own weekly patterns are proposed for independent approval.
        own_target = {**h.command("reviewer", "availability_pattern.propose", {
            "person_id": own_person, "weekdays": [2], "start_time": "13:00", "end_time": "17:00",
            "starts_on": "2026-10-01"})["pattern"], "source": "pattern"}
        rows, eligible_ids = [other, own_target], {other["id"]}
    listing = [row for row in h.get("reviewer", "/api/requests/all")["items"] if row["source"] == source]
    assert {row["id"] for row in listing} == {row["id"] for row in rows}
    eligible = [row for row in listing if row["can_decide"]]
    assert {row["id"] for row in eligible} == eligible_ids
    assert decide(h, eligible, role="reviewer")["count"] == len(eligible)
    assert not any(row["can_decide"] for row in h.get("reviewer", "/api/requests/all")["items"] if row["source"] == source)


@pytest.mark.parametrize("invalid", ["empty", "duplicate", "501", "source", "withdraw", "revision", "reason"])
def test_batch_input_is_bounded_and_validated_before_any_change(h, invalid):
    row = request_row(h)
    payload = {"items": [item(row)], "decision": "approve", "reason": REASON}
    if invalid == "empty":
        payload["items"] = []
    elif invalid == "duplicate":
        payload["items"] *= 2
    elif invalid == "501":
        payload["items"] = [{"id": str(uuid.uuid4()), "expected_revision": 1} for _ in range(501)]
    elif invalid == "source":
        payload["items"][0]["source"] = "swap"
    elif invalid == "withdraw":
        payload["decision"] = "withdraw"
    elif invalid == "revision":
        payload["items"][0].pop("expected_revision")
    else:
        payload["reason"] = ""
        payload["decision"] = "reject"  # Approvals may omit a note; refusals still need a reason.
    h.client("approver")
    before = state(h)
    h.command("approver", "request.decide_many", payload, status=422)
    assert state(h) == before


def test_source_defaults_to_request(h):
    row = request_row(h)
    reply = h.command("approver", "request.decide_many", {"items": [{"id": row["id"], "expected_revision": row["revision"]}],
        "decision": "reject", "reason": REASON})
    assert reply["records"][0]["source"] == "request" and reply["records"][0]["status"] == "rejected"


def test_single_undo_accepts_the_nullable_batch_contract(h):
    row = request_row(h)
    row = h.command("approver", "request.decide", {"decision": "approve", "reason": REASON},
                    row["id"], row["revision"])["record"]
    undone = h.command("approver", "request.undo", {"batch_id": None, "reason": "Undo single decision"},
                       row["id"], row["revision"])["record"]
    assert undone["status"] == "submitted" and undone["revision"] == 3


@pytest.mark.parametrize("failure", ["actor", "stale", "linked"])
def test_batch_undo_refusal_is_atomic(database, failure):
    h = database
    rows = [request_row(h), request_row(h)]
    reply = decide(h, rows)
    role = "approver"
    if failure == "actor":
        identity(h, "another_approver", ["approver"])
        role = "another_approver"
    elif failure == "stale":
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.RequestRecord, rows[1]["id"]).revision += 1
    else:
        row = reply["records"][1]
        h.command("coordinator", "request.link_rota", {"workspace_id": h.workspace()["id"], "reason": "Rota acts on this request"},
                  row["id"], row["revision"])
    h.client(role)
    before = state(h)
    h.command(role, "request.undo", {"batch_id": reply["batch_id"], "reason": "Undo entire batch"},
              status=403 if failure == "actor" else 409)
    assert state(h) == before


def test_batch_undo_is_scoped_to_its_original_department(h):
    rows = [request_row(h), request_row(h)]
    reply = decide(h, rows, role="admin")
    client = h.client("admin")
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed["organisation_id"], name="Other test department")
        db.add(department)
        db.flush()
        db.add(m.RoleBinding(identity_id=h.seed["identities"]["admin"], department_id=department.id,
                             role="admin", starts_on=datetime(2020, 1, 1).date()))
        session = db.scalar(select(m.Session).where(m.Session.token_hash == auth.digest(client.cookies["rota_session"])))
        session.department_id = department.id
    before = state(h)
    h.command("admin", "request.undo", {"batch_id": reply["batch_id"], "reason": "Wrong department"}, status=404)
    assert state(h) == before


@pytest.mark.parametrize("operation", ["replay", "undo", "undo_replay"])
def test_replay_and_undo_require_current_permissions_for_every_source(h, operation):
    reviewer = identity(h, "both_reviewer", ["approver", "coordinator"])
    rows = [request_row(h), pattern_row(h)]
    key = str(uuid.uuid4())
    reply = decide(h, rows, role="both_reviewer", key=key)
    undo_key = str(uuid.uuid4())
    if operation == "undo_replay":
        h.command("both_reviewer", "request.undo", {"batch_id": reply["batch_id"], "reason": "Undo mixed batch"}, key=undo_key)
    with transaction(h.app.state.engine, write=True) as db:
        binding = db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == reviewer,
                                                        m.RoleBinding.role == "coordinator")).one()
        binding.revoked = True
    before = state(h)
    if operation == "replay":
        decide(h, rows, role="both_reviewer", key=key, status=403)
    else:
        h.command("both_reviewer", "request.undo", {"batch_id": reply["batch_id"], "reason": "Undo mixed batch"},
                  status=403, key=undo_key if operation == "undo_replay" else None)
    assert state(h) == before


def test_500_decisions_and_undo_each_count_as_one_command(database, monkeypatch):
    h = database
    moment = freeze_clock(monkeypatch)
    template = request_row(h)
    start = datetime(2026, 10, 4, 6, tzinfo=timezone.utc)
    with transaction(h.app.state.engine, write=True) as db:
        made = [m.RequestRecord(department_id=h.seed["department_id"], person_id=h.seed["people"][n % len(h.seed["people"])],
                    kind="leave", leave_type_id=template["leave_type_id"], start=start + timedelta(days=n),
                    end=start + timedelta(days=n, hours=1), private_reason="Synthetic scale request",
                    created_by=h.seed["identities"]["staff"]) for n in range(500)]
        db.add_all(made)
        db.flush()
        rows = [{"id": r.id, "revision": r.revision, "source": "request"} for r in made]
    key = auth.digest(f"commands:{h.seed['identities']['approver']}:{int(moment.timestamp()) // 60 * 60}")
    reply = decide(h, rows)
    assert reply["count"] == 500 and len(reply["records"]) == 500
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RateLimit, key).hits == 1
        assert db.scalar(select(func.count()).select_from(m.RequestRecord).where(m.RequestRecord.status == "approved")) == 500
        assert db.scalar(select(func.count()).select_from(m.Availability).where(m.Availability.retracted.is_(False))) == 500
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "request.decide")))
        assert len(events) == 500 and all(e.detail["batch_id"] == reply["batch_id"] for e in events)
    undone = h.command("approver", "request.undo", {"batch_id": reply["batch_id"], "reason": "Undo 500 decisions"})
    assert undone["count"] == 500 and len(undone["records"]) == 500
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RateLimit, key).hits == 2
        assert db.scalar(select(func.count()).select_from(m.RequestRecord).where(m.RequestRecord.status == "approved")) == 0
        assert db.scalar(select(func.count()).select_from(m.Availability).where(m.Availability.retracted.is_(False))) == 0


@pytest.mark.postgres
def test_postgres_100_decisions_at_department_scale(pg, capsys):
    """A synthetic dev-sized department; report wall time without a brittle shared-workstation assertion."""
    h = pg
    workspace = h.workspace("Synthetic scale rota", "2026-10-01", "2026-11-01")
    first_shift = h.shift(workspace)
    start = datetime(2026, 10, 4, 6, tzinfo=timezone.utc)
    with transaction(h.app.state.engine, write=True) as db:
        people = [m.Person(organisation_id=h.seed["organisation_id"], display_name=f"Scale person {n:03d}")
                  for n in range(150 - len(h.seed["people"]))]
        db.add_all(people)
        db.flush()
        everyone = h.seed["people"] + [p.id for p in people]
        template = db.get(m.Shift, first_shift["shift_id"])
        shifts = [m.Shift(workspace_id=workspace["id"], assignment_type_id=template.assignment_type_id,
                          type_revision=template.type_revision, type_snapshot=template.type_snapshot,
                          location_id=template.location_id, start=start + timedelta(days=n // 18, hours=n % 8),
                          end=start + timedelta(days=n // 18, hours=n % 8 + 1)) for n in range(1, 500)]
        db.add_all(shifts)
        db.flush()
        db.add_all([m.Assignment(shift_id=shift.id, person_id=everyone[n % 150], start=shift.start, end=shift.end)
                    for n, shift in enumerate(shifts)])
        leave_type = db.scalars(select(m.LeaveType).where(m.LeaveType.department_id == h.seed["department_id"])).first()
        rows = []
        for n in range(3000):
            waiting = n >= 2900
            at = start + timedelta(days=n % 28) if waiting else datetime(2023, 1, 1, tzinfo=timezone.utc) + timedelta(days=37 * (n // 150))
            row = m.RequestRecord(department_id=h.seed["department_id"], person_id=everyone[n % 150], kind="leave",
                leave_type_id=leave_type.id, start=at, end=at + timedelta(hours=1), private_reason="Synthetic scale request",
                status="submitted" if waiting else "approved", created_by=h.seed["identities"]["staff"],
                decided_by=None if waiting else h.seed["identities"]["approver"])
            db.add(row)
            if waiting:
                rows.append(row)
        db.flush()
        items = [{"id": row.id, "revision": row.revision, "source": "request"} for row in rows]
        assert db.scalar(select(func.count()).select_from(m.Person)) == 150
        assert db.scalar(select(func.count()).select_from(m.Shift)) == 500
        assert db.scalar(select(func.count()).select_from(m.RequestRecord)) == 3000
    h.client("approver")
    began = time.perf_counter()
    reply = decide(h, items)
    approval_seconds = time.perf_counter() - began
    assert reply["count"] == 100 and all(r["stage"] == "approved" for r in reply["records"])
    began = time.perf_counter()
    undone = h.command("approver", "request.undo", {"batch_id": reply["batch_id"], "reason": "Undo scale batch"})
    undo_seconds = time.perf_counter() - began
    assert undone["count"] == 100 and all(r["stage"] == "waiting" for r in undone["records"])
    with capsys.disabled():
        print(f"\nPostgreSQL synthetic scale (150 people, 500 sessions, 3000 requests): "
              f"100 approvals {approval_seconds:.3f}s; one Undo {undo_seconds:.3f}s; target approvals <=1.5s")
