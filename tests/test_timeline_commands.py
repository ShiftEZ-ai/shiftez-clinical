"""Timeline gestures keep rotation validation, programme authority, approval and reversible audit writes."""
from datetime import date
import uuid

import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from packages.contracts import schemas as s
from tests.test_residency import other_programme, setup_residency


REASON = "Synthetic timeline gesture"


@pytest.fixture
def timeline(h):
    _, programme, cohort, types, people, blocks = setup_residency(h)
    value = {"action": "create", "person_id": people[0], "cohort_id": cohort["id"],
             "rotation_type_id": types[0]["id"], "starts_on": "2026-10-01", "ends_on": "2026-10-08", "reason": REASON}
    return h, programme, cohort, types, people, blocks, value


def gesture(h, value, role="programme_lead", status=200, key=None):
    result = h.command(role, "timeline.rotation", value, status=status, key=key)
    if status == 200:
        s.TimelineRotationResult.model_validate({key: result[key] for key in s.TimelineRotationResult.model_fields
                                               if key in result})
    return result


def check(h, value, role="programme_lead", status=200):
    response = h.client(role).post("/api/timeline/preview", json=value)
    assert response.status_code == status, response.text
    result = response.json()
    if status == 200:
        s.TimelinePreview.model_validate(result)
    return result


def undo(h, result, role="programme_lead"):
    command = result["undo"]
    return h.command(role, command["kind"], command["payload"], command.get("target_id"), command.get("expected_revision"))


def counts(h):
    with transaction(h.app.state.engine) as db:
        return {cls.__tablename__: db.scalar(select(func.count()).select_from(cls)) for cls in
                (m.Rotation, m.Mentorship, m.RequestRecord, m.DefinitionHistory, m.AuditEvent, m.CommandReceipt, m.Outbox)}


def test_create_move_resize_copy_keep_attachments_undo_and_audit(timeline):
    h, _, _, _, _, _, value = timeline
    value = {**value, "mentor_id": h.seed["people"][2], "service_id": h.seed["services"][0]}
    h.client("programme_lead")
    before = counts(h)
    preview = check(h, value)
    assert preview["can_apply"] and not preview["requires_approval"] and preview["conflicts"] == []
    assert counts(h) == before
    created = gesture(h, value)
    [rotation] = created["created"]
    assert created["status"] == "applied" and rotation["mentor_id"] == value["mentor_id"]
    moved = gesture(h, {"action": "move", "person_id": value["person_id"], "rotation_id": rotation["id"],
        "expected_revision": rotation["revision"], "starts_on": "2026-10-09", "reason": REASON})
    [current] = moved["records"]
    assert (current["starts_on"], current["ends_on"]) == ("2026-10-09", "2026-10-16")
    [attachment] = h.get("coordinator", "/api/catalogues")["mentorships"]
    assert (attachment["starts_on"], attachment["ends_on"]) == (current["starts_on"], current["ends_on"])
    resized = gesture(h, {"action": "resize", "person_id": value["person_id"], "rotation_id": current["id"],
        "expected_revision": current["revision"], "starts_on": "2026-10-09", "ends_on": "2026-10-14", "reason": REASON})
    restored = undo(h, resized)["record"]
    assert restored["ends_on"] == "2026-10-16"
    copied = gesture(h, {"action": "copy", "person_id": value["person_id"], "rotation_id": restored["id"],
        "expected_revision": restored["revision"], "starts_on": "2026-11-09", "reason": REASON})
    assert copied["created"][0]["ends_on"] == "2026-11-16"
    assert copied["created"][0]["mentor_id"] == value["mentor_id"]
    assert undo(h, copied)["removed"] == 1
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.AuditEvent).where(m.AuditEvent.action == "timeline.rotation")) == 4
        assert db.scalar(select(func.count()).select_from(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == "rotations", m.DefinitionHistory.object_id == rotation["id"])) == 4


def test_staff_drop_requests_independent_approval_and_approval_undo(timeline):
    h, _, _, _, _, _, value = timeline
    [rotation] = gesture(h, value)["created"]
    payload = {"action": "move", "person_id": rotation["person_id"], "rotation_id": rotation["id"],
        "expected_revision": rotation["revision"], "starts_on": "2026-10-09", "reason": REASON}
    h.client("staff")
    before = counts(h)
    preview = check(h, payload, "staff")
    assert preview["requires_approval"] and preview["can_apply"]
    assert counts(h) == before
    submitted = gesture(h, payload, "staff")
    assert submitted["status"] == "submitted" and submitted["records"] == []
    request = submitted["request"]
    [unchanged] = h.get("programme_lead", "/api/catalogues")["rotations"]
    assert unchanged == rotation
    h.command("staff", "request.decide", {"decision": "approve", "reason": REASON},
              request["id"], request["revision"], status=403)
    approved = h.command("programme_lead", "request.decide", {"decision": "approve", "reason": REASON},
                         request["id"], request["revision"])["record"]
    [changed] = h.get("programme_lead", "/api/catalogues")["rotations"]
    assert (changed["starts_on"], changed["ends_on"]) == ("2026-10-09", "2026-10-16")
    h.command("programme_lead", "request.undo", {"reason": REASON}, request["id"], approved["revision"])
    [restored] = h.get("programme_lead", "/api/catalogues")["rotations"]
    assert (restored["starts_on"], restored["ends_on"]) == (rotation["starts_on"], rotation["ends_on"])


def test_staff_pending_drop_is_withdrawable(timeline):
    h, _, _, _, _, _, value = timeline
    [rotation] = gesture(h, value)["created"]
    result = gesture(h, {"action": "resize", "person_id": value["person_id"], "rotation_id": rotation["id"],
        "expected_revision": rotation["revision"], "starts_on": value["starts_on"], "ends_on": "2026-10-07", "reason": REASON}, "staff")
    assert undo(h, result, "staff")["record"]["status"] == "withdrawn"
    assert h.get("programme_lead", "/api/catalogues")["rotations"] == [rotation]


def test_template_uses_saved_sequence_and_undo_is_complete(timeline):
    h, programme, cohort, types, people, blocks, _ = timeline
    template = h.configure("rotation_templates", {"name": "Synthetic persisted sequence",
        "programme_id": programme["id"], "blocks": blocks}, role="programme_lead")["record"]
    value = {"action": "template", "person_id": people[0], "cohort_id": cohort["id"],
             "template_id": template["id"], "starts_on": "2026-10-01", "reason": REASON}
    preview = check(h, value)
    assert [(r["rotation_type_id"], r["starts_on"], r["ends_on"]) for r in preview["rotations"]] == [
        (types[0]["id"], "2026-10-01", "2026-11-01"), (types[1]["id"], "2026-11-01", "2026-12-01")]
    assert h.get("programme_lead", "/api/catalogues")["rotations"] == []
    result = gesture(h, value)
    assert len(result["created"]) == 2 and result["conflicts"] == []
    assert undo(h, result)["removed"] == 2


def test_template_is_atomic_when_later_block_is_invalid(timeline):
    h, programme, cohort, _, people, blocks, value = timeline
    [existing] = gesture(h, {**value, "starts_on": "2026-11-01", "ends_on": "2026-11-08"})["created"]
    template = h.configure("rotation_templates", {"name": "Synthetic overlapping sequence", "blocks": blocks,
        "programme_id": programme["id"]}, role="programme_lead")["record"]
    payload = {"action": "template", "person_id": people[0], "cohort_id": cohort["id"],
               "template_id": template["id"], "starts_on": "2026-10-01", "reason": REASON}
    before = counts(h)
    check(h, payload, status=409)
    assert counts(h) == before
    gesture(h, payload, status=409)
    assert h.get("programme_lead", "/api/catalogues")["rotations"] == [existing]


@pytest.mark.parametrize("policy,code,allowed", [
    ("not_allowed", "ROTATION_LEAVE_NOT_ALLOWED", False),
    ("needs_approval", "ROTATION_LEAVE_NEEDS_APPROVAL", True), ("allowed", None, True)])
def test_preview_and_commit_use_shared_rotation_leave_findings(timeline, policy, code, allowed):
    h, _, _, types, _, _, value = timeline
    typ = types[0]
    h.configure("rotation_types", {"name": typ["name"], "starts_on": typ["starts_on"], "leave_policy": policy},
                typ["id"], typ["revision"])
    h.command("coordinator", "availability.record", {"person_id": value["person_id"], "kind": "off_duty",
        "start": "2026-10-03T00:00:00+03:00", "end": "2026-10-04T00:00:00+03:00", "private_reason": REASON})
    preview = check(h, value)
    assert preview["can_apply"] is allowed
    assert [f["code"] for f in preview["conflicts"]] == ([code] if code else [])
    if code:
        assert preview["conflicts"][0]["person_id"] == value["person_id"]
        assert preview["conflicts"][0]["detail"]["leave_policy"] == policy
    result = gesture(h, value, status=200 if allowed else 409)
    if allowed:
        assert result["conflicts"] == preview["conflicts"]
    else:
        assert h.get("programme_lead", "/api/catalogues")["rotations"] == []


@pytest.mark.parametrize("action", ["move", "resize", "copy"])
def test_source_revision_and_programme_authority_are_checked_in_preview_and_commit(timeline, action):
    h, _, _, types, people, _, value = timeline
    _, foreign_cohort, _ = other_programme(h, people[0])
    rotation = h.configure("rotations", {key: val for key, val in {
        **value, "cohort_id": foreign_cohort["id"]}.items() if key not in {"action", "reason"}}, role="coordinator")["record"]
    payload = {"action": action, "person_id": people[0], "rotation_id": rotation["id"],
        "expected_revision": rotation["revision"], "starts_on": "2026-11-01", "ends_on": "2026-11-08", "reason": REASON}
    check(h, payload, status=403)
    gesture(h, payload, status=403)
    stale = {**payload, "expected_revision": rotation["revision"] + 1}
    assert check(h, stale, "coordinator", status=409)["error"]["code"] == "STALE_REVISION"
    gesture(h, stale, "coordinator", status=409)


def test_recovered_receipt_rechecks_source_and_target_programme(timeline):
    h, _, _, _, _, _, value = timeline
    [rotation] = gesture(h, value)["created"]
    key = str(uuid.uuid4())
    payload = {"action": "copy", "person_id": rotation["person_id"], "rotation_id": rotation["id"],
        "expected_revision": rotation["revision"], "starts_on": "2026-11-01", "reason": REASON}
    result = gesture(h, payload, key=key)
    assert gesture(h, payload, key=key) == result
    with transaction(h.app.state.engine, write=True) as db:
        director = db.get(m.Identity, h.seed["identities"]["programme_lead"]).person_id
        db.scalar(select(m.ProgrammeMembership).where(m.ProgrammeMembership.person_id == director)).ends_on = date(2026, 2, 1)
    h.get("programme_lead", "/api/commands/" + key, status=403)
    gesture(h, payload, key=key, status=403)


@pytest.mark.parametrize("action", ["create", "copy", "template"])
def test_staff_cannot_directly_create_copy_or_place_templates(timeline, action):
    h, programme, cohort, _, people, blocks, value = timeline
    [rotation] = gesture(h, value)["created"]
    template = h.configure("rotation_templates", {"name": "Synthetic staff refusal", "blocks": blocks,
        "programme_id": programme["id"]}, role="programme_lead")["record"]
    payload = {**value, "action": action, "starts_on": "2026-11-01", "ends_on": "2026-11-08"}
    if action == "copy":
        payload.update(rotation_id=rotation["id"], expected_revision=rotation["revision"])
    if action == "template":
        payload = {"action": "template", "person_id": people[0], "cohort_id": cohort["id"],
                   "template_id": template["id"], "starts_on": "2026-11-01", "reason": REASON}
    check(h, payload, "staff", status=403)
    gesture(h, payload, "staff", status=403)


def test_preview_reports_external_rotation_conflict_from_shared_published_duty_check(timeline):
    h, _, _, _, _, _, value = timeline
    typ = h.configure("assignment_types", {"name": "Synthetic required timeline cover", "starts_on": "2026-01-01",
        "definition": {"requirements": [{"minimum": 1}]}})["record"]
    rule = h.policy({"kind": "minimum_cover", "minimum": 1}, {"service_id": h.seed["services"][0]})
    workspace = h.workspace(start="2026-10-01", end="2026-10-08")
    h.publish(h.shift(workspace, typ=typ["id"])["workspace"])
    preview = check(h, {**value, "department_available": False})
    [conflict] = [finding for finding in preview["conflicts"] if finding["code"] == "EXTERNAL_ROTATION"]
    assert conflict["person_id"] == value["person_id"] and conflict["severity"] == "error"
    assert conflict["shift_id"] and conflict["message"] and preview["can_apply"]
    coverage = next(finding for finding in preview["conflicts"]
                    if finding["code"] == "SCOPED_COVERAGE_GAP" and finding["rule_id"] == rule["id"])
    assert coverage["person_id"] is None and coverage["shift_id"] == conflict["shift_id"]
    assert coverage["cause"] and coverage["fix_hints"]
    assert any(finding["code"] == "COVERAGE_GAP" for finding in conflict["detail"]["derived_findings"])
    assert all(finding.get("cause") and finding.get("fix_hints")
               for finding in conflict["detail"]["derived_findings"] if finding["code"] == "COVERAGE_GAP")
    # Planning keeps the existing input-write semantics; live rota conflicts are visible before the drop.
    result = gesture(h, {**value, "department_available": False})
    assert result["conflicts"] == preview["conflicts"]


def test_staff_drop_cannot_reassign_person_or_attachment(timeline):
    h, _, _, _, people, _, value = timeline
    [rotation] = gesture(h, value)["created"]
    payload = {"action": "move", "person_id": value["person_id"], "rotation_id": rotation["id"],
        "expected_revision": rotation["revision"], "starts_on": "2026-10-09", "reason": REASON}
    for changes in ({"person_id": people[1]}, {"mentor_id": h.seed["people"][2]}, {"department_available": False}):
        check(h, {**payload, **changes}, "staff", status=403)
        gesture(h, {**payload, **changes}, "staff", status=403)


def test_preview_and_commit_validate_references_before_domain_evaluation(timeline):
    h, _, _, _, _, _, value = timeline
    payload = {**value, "service_id": str(uuid.uuid4())}
    assert check(h, payload, status=404)["error"]["code"] == "NOT_FOUND"
    gesture(h, payload, status=404)


def test_template_drop_rejects_fields_that_would_override_saved_blocks(timeline):
    h, programme, cohort, types, people, blocks, _ = timeline
    template = h.configure("rotation_templates", {"name": "Synthetic immutable sequence", "blocks": blocks,
        "programme_id": programme["id"]}, role="programme_lead")["record"]
    value = {"action": "template", "person_id": people[0], "cohort_id": cohort["id"],
             "template_id": template["id"], "starts_on": "2026-10-01", "reason": REASON}
    for overrides in ({"ends_on": "2026-10-20"}, {"rotation_type_id": types[1]["id"]},
                      {"service_id": h.seed["services"][0]}, {"mentor_id": h.seed["people"][2]},
                      {"department_available": False}):
        check(h, {**value, **overrides}, status=422)
        gesture(h, {**value, **overrides}, status=422)
    assert h.get("programme_lead", "/api/catalogues")["rotations"] == []


def test_reassignment_checks_source_person_and_original_rotation_window(timeline):
    h, _, _, types, people, _, value = timeline
    [rotation] = gesture(h, value)["created"]
    rule = h.policy({"kind": "restriction"}, {"conditions": {"match": "all", "conditions": [
        {"field": "rotation_type_ids", "op": "none", "values": [types[0]["id"]]}]}})
    h.publish(h.shift(h.workspace(start="2026-10-01", end="2026-10-08"))["workspace"])
    payload = {"action": "move", "person_id": people[1], "rotation_id": rotation["id"],
        "expected_revision": rotation["revision"], "starts_on": "2026-11-01", "reason": REASON}
    preview = check(h, payload)
    [conflict] = [finding for finding in preview["conflicts"]
                  if finding["code"] == "RESTRICTION" and finding["rule_id"] == rule["id"]]
    assert conflict["person_id"] == people[0] and conflict["start"].startswith("2026-10-04")
    assert gesture(h, payload)["conflicts"] == preview["conflicts"]


def test_staff_private_request_reason_stays_out_of_receipts_and_audit_including_withdrawal(timeline):
    h, _, _, _, _, _, value = timeline
    [rotation] = gesture(h, value)["created"]
    private_reason = "Synthetic private rotation change explanation"
    submitted = gesture(h, {"action": "move", "person_id": value["person_id"], "rotation_id": rotation["id"],
        "expected_revision": rotation["revision"], "starts_on": "2026-10-09", "reason": private_reason}, "staff")
    assert private_reason not in str(submitted)
    undo(h, submitted, "staff")
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RequestRecord, submitted["request"]["id"]).private_reason == private_reason
        assert all(private_reason not in str(record(row)) for cls in (m.CommandReceipt, m.AuditEvent)
                   for row in db.scalars(select(cls)))


def test_timeline_command_rejects_source_fields_in_command_envelope(timeline):
    h, _, _, _, _, _, value = timeline
    for target, revision in ((str(uuid.uuid4()), None), (None, 1)):
        reply = h.command("programme_lead", "timeline.rotation", value, target, revision, status=422)
        assert reply["error"]["code"] == "TIMELINE_COMMAND_TARGET"


def test_preview_keeps_pooled_cover_conflict_when_representative_shift_has_another_person(timeline):
    h, _, _, _, people, _, value = timeline
    rule = h.policy({"kind": "minimum_cover", "minimum": 2, "aggregation": "service_group"},
                    {"service_ids": h.seed["services"][:2]})
    workspace = h.shift(h.workspace(start="2026-10-01", end="2026-10-08"), people=[people[0]])["workspace"]
    workspace = h.shift(workspace, people=[people[1]], service=1)["workspace"]
    shifts = sorted(h.detail(workspace)["shifts"], key=lambda shift: shift["id"])
    changed_shift = shifts[-1]
    payload = {**value, "person_id": changed_shift["participants"][0]["person_id"], "department_available": False}
    h.publish(workspace)
    preview = check(h, payload)
    [finding] = [finding for finding in preview["conflicts"]
                 if finding["code"] == "SCOPED_COVERAGE_GAP" and finding["rule_id"] == rule["id"]]
    assert finding["shift_id"] == shifts[0]["id"]
    assert finding["shift_id"] != changed_shift["id"] and finding["person_id"] is None
    assert changed_shift["id"] in finding["detail"]["affected_shift_ids"]
    assert finding["cause"] and finding["fix_hints"]
