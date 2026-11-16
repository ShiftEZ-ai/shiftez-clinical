"""A governed date exchange is one atomic operation, including approval and Undo."""
from copy import deepcopy
from datetime import date
import json
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from packages.domain.rotation_swap import swapped_dates
from tests.governance_support import identity
from tests.test_residency import other_programme, setup_residency


REASON = "Synthetic rotation date exchange"


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def swap(request):
    h = request.getfixturevalue(request.param)
    _, programme, cohort, types, people, _ = setup_residency(h)
    rows = [h.configure("rotations", {"person_id": people[0], "cohort_id": cohort["id"],
        "rotation_type_id": typ["id"], "service_id": h.seed["services"][i],
        "mentor_id": people[1], "starts_on": start, "ends_on": end}, role="programme_lead")["record"]
        for i, (typ, start, end) in enumerate(zip(types, ("2026-10-01", "2026-11-01"),
                                                               ("2026-11-01", "2026-11-15")))]
    return h, programme, cohort, types, people, rows


def propose(h, rows, role="staff", status=200, key=None):
    return h.command(role, "rotation.swap", {"rotation_id": rows[0]["id"],
        "expected_revision": rows[0]["revision"], "other_rotation_id": rows[1]["id"],
        "other_expected_revision": rows[1]["revision"], "reason": REASON}, status=status, key=key)


def decide(h, row, decision="approve", role="programme_lead", status=200):
    return h.command(role, "request.decide", {"decision": decision, "reason": REASON},
                     row["id"], row["revision"], status=status)


def rotations(h, rows):
    current = {row["id"]: row for row in h.get("coordinator", "/api/catalogues")["rotations"]}
    return [current[row["id"]] for row in rows]


def test_domain_date_exchange_preserves_non_date_facts_and_inputs():
    first = {"person_id": "one", "rotation_type_id": "first", "starts_on": "2026-10-01", "ends_on": "2026-10-10"}
    second = {"person_id": "two", "rotation_type_id": "second", "starts_on": "2026-11-01", "ends_on": "2026-11-20"}
    before = deepcopy((first, second))
    changed = swapped_dates(first, second)
    assert (first, second) == before
    assert [row["starts_on"] for row in changed] == [second["starts_on"], first["starts_on"]]
    assert [row["ends_on"] for row in changed] == [second["ends_on"], first["ends_on"]]
    assert [(row["person_id"], row["rotation_type_id"]) for row in changed] == [("one", "first"), ("two", "second")]
    assert swapped_dates(*changed) == before


def test_swap_contract_rejects_duplicate_targets_and_missing_revisions():
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    value = {"rotation_id": first, "other_rotation_id": second,
             "expected_revision": 1, "other_expected_revision": 1, "reason": REASON}
    assert s.RotationSwapInput.model_validate(value)
    for invalid in ({**value, "other_rotation_id": first}, {**value, "other_expected_revision": 0},
                    {key: val for key, val in value.items() if key != "expected_revision"}):
        with pytest.raises(ValueError):
            s.RotationSwapInput.model_validate(invalid)


def test_propose_approve_undo_reapprove_retains_facts_and_attachments(swap):
    h, _, _, _, _, rows = swap
    key = str(uuid.uuid4())
    result = propose(h, rows, key=key)
    assert propose(h, rows, key=key) == result
    request = result["record"]
    assert request["kind"] == "rotation_change" and request["status"] == "submitted"
    assert rotations(h, rows) == rows
    decide(h, request, role="staff", status=403)
    approved = decide(h, request)["record"]
    changed = rotations(h, rows)
    for original, current, other in zip(rows, changed, reversed(rows)):
        assert (current["starts_on"], current["ends_on"]) == (other["starts_on"], other["ends_on"])
        assert current["revision"] == original["revision"] + 1
        assert all(current[key] == original[key] for key in ("person_id", "cohort_id", "rotation_type_id", "service_id", "mentor_id"))
    with transaction(h.app.state.engine) as db:
        for row in changed:
            rotation = db.get(m.Rotation, row["id"])
            attachment = rotation.attachment_link.mentorship
            assert (attachment.starts_on.isoformat(), attachment.ends_on.isoformat()) == (row["starts_on"], row["ends_on"])
    undone = h.command("programme_lead", "request.undo", {"reason": REASON}, request["id"], approved["revision"])["record"]
    assert [(r["starts_on"], r["ends_on"]) for r in rotations(h, rows)] == [(r["starts_on"], r["ends_on"]) for r in rows]
    assert decide(h, undone)["record"]["status"] == "approved"


def test_withdrawal_and_pending_change_on_either_half(swap):
    h, _, _, _, _, rows = swap
    result = propose(h, rows)
    propose(h, list(reversed(rows)), status=409)
    for row in rows:
        h.command("staff", "rotation.request_change", {"rotation_id": row["id"],
            "rotation_type_id": row["rotation_type_id"], "service_id": row["service_id"],
            "starts_on": "2026-12-01", "ends_on": "2026-12-15", "private_reason": REASON}, status=409)
    undo = result["undo"]
    withdrawn = h.command("staff", undo["kind"], undo["payload"], undo["target_id"], undo["expected_revision"])["record"]
    assert withdrawn["status"] == "withdrawn" and rotations(h, rows) == rows
    assert propose(h, rows)["record"]["status"] == "submitted"


@pytest.mark.parametrize("phase", ["submitted", "approved", "withdrawn"])
@pytest.mark.parametrize("kind", ["rotation.remove", "rotation.undo_plan"])
def test_swap_keeps_both_referenced_rotations(swap, phase, kind):
    h, _, _, _, _, rows = swap
    pending = propose(h, rows)["record"]
    if phase != "submitted":
        decide(h, pending, "approve" if phase == "approved" else "withdraw",
               role="programme_lead" if phase == "approved" else "staff")
    before = rotations(h, rows)
    removed = h.command("programme_lead", kind, {"items": [
        {"id": row["id"], "revision": row["revision"]} for row in before], "reason": REASON})
    assert removed["removed"] == 0 and removed["kept"] == 2
    assert rotations(h, rows) == before


@pytest.mark.parametrize("phase", ["proposal", "approval", "undo"])
def test_stale_second_rotation_rejects_whole_operation(swap, phase):
    h, _, _, _, _, rows = swap
    pending = propose(h, rows)["record"] if phase != "proposal" else None
    if phase == "undo":
        pending = decide(h, pending)["record"]
    current = rotations(h, rows)
    other = current[1]
    h.configure("rotations", {key: other[key] for key in s.RotationInput.model_fields},
                other["id"], other["revision"], role="coordinator")
    before = rotations(h, rows)
    if phase == "proposal":
        propose(h, rows, status=409)
    elif phase == "approval":
        decide(h, pending, status=409)
    else:
        h.command("programme_lead", "request.undo", {"reason": REASON}, pending["id"], pending["revision"], status=409)
    assert rotations(h, rows) == before
    if pending:
        with transaction(h.app.state.engine) as db:
            assert db.get(m.RequestRecord, pending["id"]).status == pending["status"]


def test_swap_checks_both_programmes_and_staff_ownership(swap):
    h, _, _, types, people, rows = swap
    _, cohort, _ = other_programme(h, people[1])
    foreign = h.configure("rotations", {"person_id": people[1], "cohort_id": cohort["id"],
        "rotation_type_id": types[0]["id"], "starts_on": "2026-12-01", "ends_on": "2026-12-15"}, role="coordinator")["record"]
    propose(h, [rows[0], foreign], role="staff", status=403)
    propose(h, [rows[0], foreign], role="programme_lead", status=403)
    requested = propose(h, [rows[0], foreign], role="coordinator")["record"]
    decide(h, requested, status=403)
    listed = h.get("programme_lead", "/api/requests/all")["items"]
    assert requested["id"] not in {row["id"] for row in listed}
    assert decide(h, requested, role="approver")["record"]["status"] == "approved"


def test_final_pair_checks_overlap_cohort_membership_and_leave(swap):
    h, _, cohort, types, people, rows = swap
    # Moving a different trainee's block into an occupied period must reject before creating a request.
    peer = h.configure("rotations", {"person_id": people[1], "cohort_id": cohort["id"],
        "rotation_type_id": types[1]["id"], "starts_on": "2026-12-01", "ends_on": "2026-12-15"}, role="coordinator")["record"]
    h.configure("rotations", {"person_id": people[1], "cohort_id": cohort["id"],
        "rotation_type_id": types[0]["id"], "starts_on": "2026-10-01", "ends_on": "2026-10-15"}, role="coordinator")
    assert propose(h, [rows[0], peer], role="coordinator", status=409)["error"]["code"] == "ROTATION_OVERLAP"
    pending = propose(h, rows)["record"]
    h.command("coordinator", "availability.record", {"person_id": people[0], "kind": "off_duty",
        "start": "2026-11-02T00:00:00+03:00", "end": "2026-11-03T00:00:00+03:00", "private_reason": REASON})
    assert decide(h, pending, status=409)["error"]["code"] == "ROTATION_LEAVE_NOT_ALLOWED"
    assert rotations(h, rows) == rows


def test_swap_cannot_be_self_approved_through_other_trainee(swap):
    h, _, cohort, types, people, rows = swap
    peer = h.configure("rotations", {"person_id": people[1], "cohort_id": cohort["id"],
        "rotation_type_id": types[1]["id"], "starts_on": "2026-12-01", "ends_on": "2026-12-15"}, role="coordinator")["record"]
    identity(h, "peer_approver", ["approver"], person=people[1])
    before = h.get("peer_approver", "/api/requests/all")["counts"]
    pending = propose(h, [rows[0], peer], role="coordinator")["record"]
    assert decide(h, pending, role="peer_approver", status=403)["error"]["code"] == "SELF_APPROVAL"
    result = h.get("peer_approver", "/api/requests/all")
    assert result["counts"]["to_decide"] == before["to_decide"]
    assert result["counts"]["stages"]["waiting"] == before["stages"]["waiting"] + 1
    assert not next(row for row in result["items"] if row["id"] == pending["id"])["can_decide"]
    assert rotations(h, rows) == rows


@pytest.mark.parametrize("service", [None, 0])
def test_swap_and_undo_stage_both_owned_attachments_in_same_scope(swap, service):
    h, _, _, _, _, rows = swap
    rows = [h.configure("rotations", {**{key: row[key] for key in s.RotationInput.model_fields},
        "service_id": h.seed["services"][service] if service is not None else None},
        row["id"], row["revision"], role="coordinator")["record"] for row in rows]
    approved = decide(h, propose(h, rows)["record"])["record"]
    changed = rotations(h, rows)
    assert [(r["starts_on"], r["ends_on"]) for r in changed] == [(r["starts_on"], r["ends_on"]) for r in reversed(rows)]
    h.command("programme_lead", "request.undo", {"reason": REASON}, approved["id"], approved["revision"])
    assert [(r["starts_on"], r["ends_on"]) for r in rotations(h, rows)] == [(r["starts_on"], r["ends_on"]) for r in rows]


def test_swap_receipt_and_decisions_recheck_both_current_programmes(swap):
    h, _, _, types, people, rows = swap
    _, cohort, _ = other_programme(h, people[1])
    foreign = h.configure("rotations", {"person_id": people[1], "cohort_id": cohort["id"],
        "rotation_type_id": types[0]["id"], "starts_on": "2026-12-01", "ends_on": "2026-12-15"}, role="coordinator")["record"]
    # The director initially owns both programmes; receipt recovery must lose that access when ownership ends.
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Programme, cohort["programme_id"]).owner_identity_id = h.seed["identities"]["programme_lead"]
    key = str(uuid.uuid4())
    pending = propose(h, [rows[0], foreign], role="programme_lead", key=key)["record"]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Programme, cohort["programme_id"]).owner_identity_id = None
    h.get("programme_lead", "/api/commands/" + key, status=403)
    propose(h, [rows[0], foreign], role="programme_lead", key=key, status=403)
    decide(h, pending, status=403)
    declined = decide(h, pending, decision="reject", role="approver")["record"]
    assert declined["status"] == "rejected" and rotations(h, rows) == rows


def test_pending_swap_projects_both_halves_with_one_decision(swap):
    h, _, _, _, _, rows = swap
    pending = propose(h, rows)["record"]
    params = "start=2026-10-01&end=2027-01-01"
    result = h.get("programme_lead", "/api/timeline?" + params)
    parts = [item for person in result["people"] for item in person["items"]
             if item["kind"] == "pending" and item["id"] == pending["id"]]
    assert {part["details"]["rotation_id"] for part in parts} == {row["id"] for row in rows}
    assert all(part["details"]["can_decide"] for part in parts)
    assert all(part["details"]["created_by"] == pending["created_by"] for part in parts)
    assert {part["details"]["swap_rotation_id"] for part in parts} == {row["id"] for row in rows}
    for part in parts:
        assert (part["starts_on"], part["ends_on"]) == (
            part["details"]["proposed"]["starts_on"], part["details"]["proposed"]["ends_on"])
    board = h.get("programme_lead", "/api/rotation-board?" + params)
    assert len(board["changes"]) == 2 and {part["id"] for part in board["changes"]} == {pending["id"]}
    october = h.get("programme_lead", "/api/timeline?start=2026-10-01&end=2026-11-01")
    [part] = [item for person in october["people"] for item in person["items"] if item["id"] == pending["id"]]
    assert part["details"]["rotation_id"] == rows[1]["id"]
    assert (part["starts_on"], part["ends_on"]) == (rows[0]["starts_on"], rows[0]["ends_on"])
    decide(h, pending)
    assert not h.get("programme_lead", "/api/rotation-board?" + params)["changes"]


def test_board_keeps_changes_proposed_into_window_from_a_rotation_outside_it(swap):
    h, _, _, _, _, rows = swap
    row = rows[0]
    pending = h.command("staff", "rotation.request_change", {"rotation_id": row["id"],
        "rotation_type_id": row["rotation_type_id"], "service_id": row["service_id"],
        "starts_on": "2026-12-01", "ends_on": "2026-12-15", "private_reason": REASON})["record"]
    board = h.get("programme_lead", "/api/rotation-board?start=2026-12-01&end=2027-01-01")
    assert board["rotations"] == []
    assert [part["id"] for part in board["changes"]] == [pending["id"]]


def test_peer_trainee_sees_swap_progress_and_receives_notifications(swap):
    from apps.worker.main import deliver_notifications

    h, _, cohort, types, people, rows = swap
    peer = h.configure("rotations", {"person_id": people[1], "cohort_id": cohort["id"],
        "rotation_type_id": types[1]["id"], "starts_on": "2026-12-01", "ends_on": "2026-12-15"}, role="coordinator")["record"]
    peer_identity = identity(h, "peer_staff", ["staff"], person=people[1])
    identity(h, "unrelated_staff", ["staff"], person=h.seed["people"][2])
    pending = propose(h, [rows[0], peer], role="coordinator")["record"]
    for role in ("peer_staff", "coordinator"):
        result = h.get(role, f"/api/timeline?start=2026-10-01&end=2027-01-01&person_ids={people[1]}")
        [part] = [item for person in result["people"] for item in person["items"] if item["id"] == pending["id"]]
        assert part["details"]["rotation_id"] == peer["id"]
        assert part["details"]["proposed"]["starts_on"] == rows[0]["starts_on"]
        assert "swap_rotation_id" not in part["details"]
    assert pending["id"] in {row["id"] for row in h.get("peer_staff", "/api/requests")["requests"]}
    listed = h.get("peer_staff", "/api/requests/all")["items"]
    assert pending["id"] in {row["id"] for row in listed}
    assert not next(row for row in listed if row["id"] == pending["id"])["can_decide"]
    with transaction(h.app.state.engine) as db:
        peer_name = db.get(m.Person, people[1]).display_name
    for params in ({"q": peer_name}, {"person_id": people[1]}, {"filter": json.dumps({"conditions": {
            "conditions": [{"field": "person_id", "values": [people[1]]}]}})}):
        response = h.client("coordinator").get("/api/requests/all", params=params)
        assert response.status_code == 200, response.text
        assert pending["id"] in {row["id"] for row in response.json()["items"]}
    assert pending["id"] not in {row["id"] for row in h.get("unrelated_staff", "/api/requests/all")["items"]}
    with transaction(h.app.state.engine, write=True) as db:
        event = db.scalar(select(m.Outbox).where(m.Outbox.payload["request_id"].as_string() == pending["id"]))
        deliver_notifications(db, event)
        note = db.scalar(select(m.Notification).where(m.Notification.outbox_id == event.id,
                                                      m.Notification.identity_id == peer_identity))
        assert note is not None and note.title.startswith("Your rotation change request")


def test_peer_request_access_survives_rotation_reassignment_without_disclosure(swap):
    from apps.worker.main import recipients_of, department_roles

    h, programme, cohort, types, people, rows = swap
    replacement = h.seed["people"][2]
    h.configure("programme_memberships", {"person_id": replacement, "programme_id": programme["id"],
        "starts_on": "2026-07-01", "ends_on": "2028-07-01"}, role="coordinator")
    peer = h.configure("rotations", {"person_id": people[1], "cohort_id": cohort["id"],
        "rotation_type_id": types[1]["id"], "starts_on": "2026-12-01", "ends_on": "2026-12-15"}, role="coordinator")["record"]
    original_identity = identity(h, "original_peer", ["staff", "approver"], person=people[1])
    new_identity = identity(h, "replacement_peer", ["staff"], person=replacement)
    pending = propose(h, [rows[0], peer], role="coordinator")["record"]
    h.configure("rotations", {**{key: peer[key] for key in s.RotationInput.model_fields}, "person_id": replacement},
                peer["id"], peer["revision"], role="coordinator")
    for role, visible in (("original_peer", True), ("replacement_peer", False)):
        assert (pending["id"] in {row["id"] for row in h.get(role, "/api/requests")["requests"]}) is visible
        assert (pending["id"] in {row["id"] for row in h.get(role, "/api/requests/all")["items"]}) is visible
    timeline = h.get("replacement_peer", "/api/timeline?start=2026-10-01&end=2027-01-01")
    assert not any(item["id"] == pending["id"] for person in timeline["people"] for item in person["items"])
    assert decide(h, pending, role="original_peer", status=403)["error"]["code"] == "SELF_APPROVAL"
    assert decide(h, pending, status=409)["error"]["code"] == "ROTATION_CHANGED"
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.Outbox).where(m.Outbox.payload["request_id"].as_string() == pending["id"]))
        recipients = recipients_of(db, event, None, department_roles(db, h.seed["department_id"]))
        assert original_identity in recipients and new_identity not in recipients


@pytest.mark.cpu_budget
def test_swap_request_scope_keeps_department_scale_read_budget(h):
    import coverage
    import time
    from datetime import datetime, timezone
    from sqlalchemy import event

    _, _, cohort, types, people, _ = setup_residency(h)
    rows = [h.configure("rotations", {"person_id": people[0], "cohort_id": cohort["id"],
        "rotation_type_id": types[0]["id"], "starts_on": start, "ends_on": end}, role="coordinator")["record"]
        for start, end in (("2026-10-01", "2026-11-01"), ("2026-11-01", "2026-12-01"))]
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(m.RequestRecord(department_id=h.seed["department_id"], person_id=people[0], kind="rotation_change",
            rotation_id=rows[0]["id"], created_by=h.seed["identities"]["staff"], private_reason=REASON,
            start=datetime(2026, 10, 1, tzinfo=timezone.utc), end=datetime(2026, 12, 1, tzinfo=timezone.utc),
            details={"proposed": {"starts_on": "2026-11-01", "ends_on": "2026-12-01"},
                     "swap": {"rotation_id": rows[1]["id"], "person_id": people[0],
                              "proposed": {"starts_on": "2026-10-01", "ends_on": "2026-11-01"}}}) for _ in range(1000))
    h.client("programme_lead")
    selects = []

    def count_sql(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(h.app.state.engine, "before_cursor_execute", count_sql)
    try:
        h.get("programme_lead", "/api/requests/all?limit=1")
        small = len(selects)
        selects.clear()
        started = time.process_time()
        result = h.get("programme_lead", "/api/requests/all?limit=100")
        elapsed = time.process_time() - started
        assert len(result["items"]) == 100 and all(row["can_decide"] for row in result["items"])
        assert len(selects) <= small + 2, (small, len(selects))
        assert elapsed < 2 * (3 if coverage.Coverage.current() else 1), elapsed
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", count_sql)
