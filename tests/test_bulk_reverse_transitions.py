"""Same-record reverses retain all source-handler checks and never approve a proposal."""
from sqlalchemy import select
import pytest
import uuid

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_availability_patterns import pattern
from .test_staff_wishes import wish


REASON = {"reason": "Undo the synthetic change"}


def transition(h, role, kind, row, *, status=200, payload=None):
    return h.command(role, kind, payload or REASON, row["id"], row["revision"], status=status)


def proposal(h, **changes):
    return h.command("coordinator", "policy.create", {"name": "Restorable synthetic proposal", "owner": "Test owner",
        "source": "Synthetic proposal, not hospital policy", "starts_on": "2026-10-01",
        "definition": {"severity": "mandatory", "scope": {}, "rule": {"kind": "min_rest", "hours": 10}},
        **changes})["record"]


def test_wish_restore_retains_same_record_and_checks_ownership_and_revision(h):
    row = wish(h, "2026-10-05", points=3, note="Original note")["wish"]
    ended = transition(h, "staff", "wish.withdraw", row)["wish"]
    identity(h, "other_staff", ["staff"], person=h.seed["people"][1])
    assert transition(h, "other_staff", "wish.restore", ended, status=403)["error"]["code"] == "FORBIDDEN"
    restored = transition(h, "staff", "wish.restore", ended)["wish"]
    assert all(restored[key] == row[key] for key in ("id", "person_id", "first_day", "last_day", "kind", "points", "note", "created_by"))
    assert restored["status"] == "open" and restored["revision"] == ended["revision"] + 1
    assert transition(h, "staff", "wish.restore", ended, status=409)["error"]["code"] == "STALE_REVISION"
    assert transition(h, "staff", "wish.restore", restored, status=409)["error"]["code"] == "WORKFLOW_STATE"


@pytest.mark.parametrize("obstacle,code", [("overlap", "WISH_OVERLAP"), ("budget", "WISH_BUDGET"),
                                           ("leave", "WISH_ON_LEAVE"), ("locked", "WISH_LOCKED")])
def test_wish_restore_reuses_current_wish_admission_checks(h, obstacle, code):
    ended = transition(h, "staff", "wish.withdraw", wish(h, "2026-10-05", points=3)["wish"])["wish"]
    if obstacle == "overlap":
        wish(h, "2026-10-05")
    elif obstacle == "budget":
        wish(h, "2026-10-06", points=8)
    elif obstacle == "leave":
        h.command("coordinator", "availability.record", {"person_id": ended["person_id"], "kind": "recorded_leave",
            "start": "2026-10-05T00:00:00+03:00", "end": "2026-10-06T00:00:00+03:00", "private_reason": "Synthetic leave"})
    else:
        h.lifecycle(h.shift(h.workspace())["workspace"], "submit")
    assert transition(h, "staff", "wish.restore", ended, status=409)["error"]["code"] == code
    with transaction(h.app.state.engine) as db:
        assert db.get(m.StaffWish, ended["id"]).status == "withdrawn"


def role_binding(h, role="programme_lead"):
    return h.command("admin", "role.grant", {"identity_id": h.seed["identities"]["staff"], "role": role,
        "starts_on": "2020-01-01", "reason": "Synthetic access binding"})["record"]


def test_role_restore_reuses_binding_and_grant_validation_and_history(h):
    row = role_binding(h)
    revoked = transition(h, "admin", "role.revoke", row)["record"]
    transition(h, "coordinator", "role.restore", revoked, status=403)
    restored = transition(h, "admin", "role.restore", revoked)["record"]
    assert all(restored[key] == row[key] for key in ("id", "identity_id", "role", "starts_on", "ends_on"))
    assert not restored["revoked"] and restored["revision"] == revoked["revision"] + 1
    transition(h, "admin", "role.restore", revoked, status=409)
    revoked = transition(h, "admin", "role.revoke", restored)["record"]
    role_binding(h)
    assert transition(h, "admin", "role.restore", revoked, status=409)["error"]["code"] == "DUPLICATE_ROLE"
    with transaction(h.app.state.engine) as db:
        versions = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == row["id"])
                                   .order_by(m.DefinitionHistory.object_revision)))
        assert [r.snapshot["revoked"] for r in versions] == [False, True, False, True]


def test_role_restore_cannot_reenable_a_disabled_sign_in(h):
    revoked = transition(h, "admin", "role.revoke", role_binding(h))["record"]
    h.command("admin", "identity.disable", REASON, h.seed["identities"]["staff"], 1)
    assert transition(h, "admin", "role.restore", revoked, status=404)["error"]["code"] == "NOT_FOUND"


def test_role_revoke_still_preserves_the_last_administrator(h):
    own = next(b for b in h.get("admin", "/api/access")["bindings"] if b["role"] == "admin" and not b["revoked"])
    assert transition(h, "admin", "role.revoke", own, status=409)["error"]["code"] == "LAST_ADMIN"


@pytest.mark.parametrize("role,original_status", [("staff", "proposed"), ("coordinator", "active")])
def test_pattern_restore_infers_previous_status_and_retains_decision(h, role, original_status):
    row = h.command(role, "availability_pattern.propose", pattern(h.seed["people"][0]))["pattern"]
    assert row["status"] == original_status
    ended = transition(h, "staff", "availability_pattern.end", row)["pattern"]
    transition(h, "staff", "availability_pattern.restore", ended, status=422, payload={**REASON, "status": "active"})
    restored = transition(h, "staff", "availability_pattern.restore", ended)["pattern"]
    assert restored["status"] == original_status and restored["decided_by"] == row["decided_by"]
    assert restored["id"] == row["id"] and restored["revision"] == ended["revision"] + 1
    transition(h, "staff", "availability_pattern.restore", ended, status=409)
    # A second end/restore round trip uses the new retained revision, with no duplicate history.
    ended = transition(h, "staff", "availability_pattern.end", restored)["pattern"]
    assert transition(h, "staff", "availability_pattern.restore", ended)["pattern"]["status"] == original_status


def test_pattern_restore_refuses_missing_prior_state_and_other_person(h):
    row = h.command("staff", "availability_pattern.propose", pattern(h.seed["people"][0]))["pattern"]
    ended = transition(h, "staff", "availability_pattern.end", row)["pattern"]
    identity(h, "other_staff", ["staff"], person=h.seed["people"][1])
    transition(h, "other_staff", "availability_pattern.restore", ended, status=403)
    with transaction(h.app.state.engine, write=True) as db:
        legacy = db.get(m.AvailabilityPattern, row["id"])
        legacy.revision += 1  # An ended legacy row has no trusted prior state for this revision.
    assert transition(h, "staff", "availability_pattern.restore", {**ended, "revision": 3}, status=409)["error"]["code"] == "WORKFLOW_STATE"


def test_declined_policy_restore_only_returns_to_proposed_and_preserves_independence(h):
    row = proposal(h)
    declined = transition(h, "coordinator", "policy.decline", row)["record"]
    assert transition(h, "approver", "policy.restore", declined, status=409)["error"]["code"] == "WORKFLOW_STATE"
    transition(h, "staff", "policy.restore_proposal", declined, status=403)
    restored = transition(h, "coordinator", "policy.restore_proposal", declined)["record"]
    assert restored["status"] == "proposed" and restored["approved_by"] is None
    assert restored["id"] == row["id"] and restored["active"] == row["active"]
    assert transition(h, "admin", "policy.restore_proposal", declined, status=409)["error"]["code"] == "STALE_REVISION"
    identity(h, "proposing_approver", ["coordinator", "approver"])
    own = h.command("proposing_approver", "policy.create", {key: row[key] for key in
                    ("name", "owner", "source", "starts_on", "ends_on", "active", "definition")})["record"]
    own = transition(h, "proposing_approver", "policy.decline", own)["record"]
    own = transition(h, "proposing_approver", "policy.restore_proposal", own)["record"]
    assert transition(h, "proposing_approver", "policy.activate", own, status=403)["error"]["code"] == "SELF_APPROVAL"
    active = transition(h, "approver", "policy.activate", restored)["record"]
    retired = transition(h, "approver", "policy.retire", active)["record"]
    assert transition(h, "approver", "policy.restore_proposal", retired, status=409)["error"]["code"] == "WORKFLOW_STATE"


def test_catalogue_proposal_restore_refuses_a_waiting_or_newer_version(h):
    from .test_rule_catalogue_conflicts import line, most, propose
    first = propose(h, "duties_per_week", [line(numbers=most(5))])["record"]
    declined = transition(h, "coordinator", "policy.decline", first)["record"]
    newer = propose(h, "duties_per_week", [line(numbers=most(4))])["record"]
    assert transition(h, "coordinator", "policy.restore_proposal", declined, status=409)["error"]["code"] == "PROPOSAL_WAITING"
    transition(h, "approver", "policy.activate", newer)
    assert transition(h, "coordinator", "policy.restore_proposal", declined, status=409)["error"]["code"] == "STALE_REVISION"


@pytest.mark.parametrize("kind,role,key", [("wish.withdraw", "staff", "wish"),
    ("availability_pattern.end", "staff", "pattern"), ("role.revoke", "admin", "record"),
    ("policy.decline", "coordinator", "record")])
def test_bulk_checked_reverse_restores_original_row_and_can_itself_be_undone(h, kind, role, key):
    if kind == "wish.withdraw":
        row = wish(h, "2026-10-05")[key]
        state = "status"
    elif kind == "availability_pattern.end":
        row = h.command("staff", "availability_pattern.propose", pattern(h.seed["people"][0]))[key]
        state = "status"
    elif kind == "role.revoke":
        row, state = role_binding(h), "revoked"
    else:
        row, state = proposal(h), "status"
    applied = h.command(role, "bulk.apply", {"item_kind": kind, "items": [{"target_id": row["id"],
        "expected_revision": row["revision"], "payload": REASON}], **REASON})
    undone = h.command(role, applied["undo"]["kind"], applied["undo"]["payload"])
    with transaction(h.app.state.engine) as db:
        model = {"wish.withdraw": m.StaffWish, "availability_pattern.end": m.AvailabilityPattern,
                 "role.revoke": m.RoleBinding, "policy.decline": m.Policy}[kind]
        restored = db.get(model, row["id"])
        assert getattr(restored, state) == row[state] and restored.revision == row["revision"] + 2
    h.command(role, undone["undo"]["kind"], undone["undo"]["payload"])
    with transaction(h.app.state.engine) as db:
        assert getattr(db.get(model, row["id"]), state) != row[state]


def checked_rest_review(h, severity):
    from tests.outbox_support import process_one_after_undo as process_one
    from .test_contextual_feasibility import use_checked_activation
    use_checked_activation(h)
    workspace = h.shift(h.workspace(), start="2026-10-04T16:00:00+03:00", end="2026-10-05T00:00:00+03:00")["workspace"]
    workspace = h.shift(workspace, start="2026-10-05T16:00:00+03:00", end="2026-10-05T20:00:00+03:00")["workspace"]
    policy = proposal(h, definition={"severity": severity, "scope": {"person_id": h.seed["people"][0]},
                                     "rule": {"kind": "min_rest", "hours": 24}})
    job = h.command("approver", "job.create", {"workspace_id": workspace["id"], "kind": "feasibility",
        "rule_changes": [{"policy_id": policy["id"], "expected_revision": policy["revision"]}]},
        revision=workspace["revision"])["job"]
    assert process_one(h.app.state.engine)
    checked = h.get("approver", "/api/jobs/" + job["id"])["job"]
    assert checked["result"]["comparison_complete"]
    assert checked["result"]["checks"]["proposed"]["status"] == "candidate_feasible"
    return policy, checked


def activation_payload(policy, checked):
    return {"item_kind": "policy.activate", "items": [{"target_id": policy["id"],
        "expected_revision": policy["revision"], "payload": {"check_job_ids": [checked["id"]]}}], **REASON}


@pytest.mark.parametrize('mode', ['single', 'bulk'])
def test_real_advisory_findings_already_allow_single_and_bulk_activation(h, mode):
    from .test_bulk import preview
    policy, checked = checked_rest_review(h, "advisory")
    assert 'findings' not in checked['result']['checks']['proposed']
    result = h.get('approver', '/api/jobs/' + checked['id'] + '?full=true')['job']['result']
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Job, checked['id']).result
    assert result['checks']['proposed']['findings'] == stored['checks']['proposed']['findings']
    assert any(f["code"] == "MIN_REST" and f["severity"] == "warning"
               for f in result["checks"]["proposed"]["findings"])
    assert result["no_new_problems"] and result["new_or_worse_findings"] == []
    check = preview(h, "approver", activation_payload(policy, checked))
    assert check["counts"]["ready"] == 1 and check["items"][0]["issues"] == []
    if mode == 'single':
        transition(h, 'approver', 'policy.activate', policy, payload={**REASON, 'check_job_ids': [checked['id']]})
    else:
        assert h.command('approver', 'bulk.apply', activation_payload(policy, checked))['count'] == 1
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, policy['id']).status == 'active'
        assert db.get(m.Job, checked['id']).result == stored
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'policy.activate',
                                                    m.AuditEvent.target_id == policy['id']))
        evidence = audit.detail['context_check_evidence'][0]
        assert evidence['check_job_id'] == checked['id'] and evidence['no_new_problems']


def test_advisory_receipt_acknowledgement_offers_resolution_and_records_finding_references(h):
    from copy import deepcopy
    from .test_bulk import preview
    policy, checked = checked_rest_review(h, "advisory")
    with transaction(h.app.state.engine, write=True) as db:
        job = db.get(m.Job, checked["id"])
        result = deepcopy(job.result)
        # Exercise a receipt requiring advisory acknowledgement; the current worker excludes warnings from comparison.
        result["new_or_worse_findings"] = [f for f in result["checks"]["proposed"]["findings"]
                                          if f["rule_id"] == policy["id"]]
        result["no_new_problems"] = False
        job.result = result
    single = transition(h, "approver", "policy.activate", policy, status=409,
                        payload={**REASON, "check_job_ids": [checked["id"]]})
    issue = single["error"]
    assert issue["code"] == "RULE_CHECK_UNRESOLVED" and issue["details"]["severity"] == "advisory"
    assert issue["details"]["findings"][0]["rule_id"] == policy["id"]
    payload = activation_payload(policy, checked)
    check = preview(h, "approver", payload)
    assert [r["id"] for r in check["items"][0]["issues"][0]["resolutions"]] == ["apply_anyway", "skip"]
    reply = h.command("approver", "bulk.apply", {**payload, "resolutions": {"0": "apply_anyway"}})
    assert reply["count"] == 1
    with transaction(h.app.state.engine) as db:
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == "policy.activate",
                                                    m.AuditEvent.target_id == policy["id"]))
        evidence = audit.detail["context_check_evidence"][0]
        assert evidence["severity"] == "advisory" and evidence["check_job_id"] == checked["id"]
        assert evidence["unresolved_findings"] == issue["details"]["findings"]
        assert audit.detail["resolved_finding"]["resolution"] == "apply_anyway"
        assert db.get(m.Policy, policy["id"]).status == "active"


@pytest.mark.parametrize("case", ["mandatory", "unknown", "mandatory_derived", "unknown_requirement"])
def test_mandatory_or_unknown_findings_never_offer_apply_anyway(h, case):
    from copy import deepcopy
    from .test_bulk import preview
    policy, checked = checked_rest_review(h, "mandatory" if case == "mandatory" else "advisory")
    if case != "mandatory":
        with transaction(h.app.state.engine, write=True) as db:
            job = db.get(m.Job, checked["id"])
            result = deepcopy(job.result)
            findings = [f for f in result["checks"]["proposed"]["findings"] if f["rule_id"] == policy["id"]]
            result.update(no_new_problems=False, new_or_worse_findings=findings)
            if case == "unknown":
                result["comparison_complete"] = False
                result["checks"]["baseline"]["status"] = "unknown_time_limit"
            elif case == "mandatory_derived":
                findings[0]["detail"]["derived_findings"] = [{"severity": "error", "detail": {"rule_severity": "mandatory"}}]
            else:
                findings[0]["requirements"] = [{"kind": "duty", "definition": {}}]
            job.result = result
    single = transition(h, "approver", "policy.activate", policy, status=409,
                        payload={**REASON, "check_job_ids": [checked["id"]]})
    assert single["error"]["code"] == "RULE_CHECK_UNRESOLVED"
    assert "severity" not in single["error"]["details"]
    check = preview(h, "approver", activation_payload(policy, checked))
    assert [r["id"] for r in check["items"][0]["issues"][0]["resolutions"]] == ["skip"]


FACT_FAMILIES = ("person.workload", "person.overtime", "calendar.protection")


def fact_payload(h, family):
    from .test_scheduling_facts_api import workload
    if family == "person.workload":
        return workload(h)
    overtime = family == "person.overtime"
    return {"kind": "overtime_offer" if overtime else "protected_time", "person_id": h.seed["people"][0],
        "catalogue_id": "overtime_offers" if overtime else "protected_time", "line": {
            "id": str(uuid.uuid4()), "weekdays": [1], "start_time": "09:00", "end_time": "11:00",
            "numbers": {"apply": {"value": None, "level": "medium" if overtime else "mandatory"}}}, **REASON}


@pytest.mark.parametrize("family", FACT_FAMILIES)
def test_fact_restore_proposal_keeps_identity_and_independent_approval(h, family):
    identity(h, "same_person_proposer", ["coordinator", "approver"], person=h.seed["people"][0])
    identity(h, "same_person_approver", ["approver"], person=h.seed["people"][0])
    row = h.command("same_person_proposer", family + ".propose", fact_payload(h, family))["fact"]
    declined = transition(h, "coordinator", family + ".decline", row)["fact"]
    transition(h, "staff", family + ".restore_proposal", declined, status=403)
    restored = transition(h, "coordinator", family + ".restore_proposal", declined)["fact"]
    assert restored["status"] == "proposed" and restored["approved_by"] is None
    assert restored["id"] == row["id"] and restored["proposed_by"] == row["proposed_by"]
    assert restored["data"] == row["data"] and restored["revision"] == declined["revision"] + 1
    assert transition(h, "coordinator", family + ".restore_proposal", declined, status=409)["error"]["code"] == "STALE_REVISION"
    refused = transition(h, "same_person_approver", family + ".activate", restored, status=403)
    assert refused["error"]["code"] == "SELF_APPROVAL"
    active = transition(h, "approver", family + ".activate", restored)["fact"]
    assert active["status"] == "active"
    assert transition(h, "approver", family + ".restore_proposal", active, status=409)["error"]["code"] == "WORKFLOW_STATE"


@pytest.mark.parametrize("family", FACT_FAMILIES)
def test_bulk_decline_undo_restores_same_fact_as_a_proposal(h, family):
    row = h.command("coordinator", family + ".propose", fact_payload(h, family))["fact"]
    applied = h.command("coordinator", "bulk.apply", {"item_kind": family + ".decline", "items": [
        {"target_id": row["id"], "expected_revision": row["revision"], "payload": REASON}], **REASON})
    assert applied["undo"]["payload"]["items"][0]["item_kind"] == family + ".restore_proposal"
    restored = h.command("coordinator", "bulk.apply", applied["undo"]["payload"])
    with transaction(h.app.state.engine) as db:
        fact = db.get(m.SchedulingFact, row["id"])
        assert fact.status == "proposed" and fact.approved_by is None and fact.revision == 3
        assert list(db.scalars(select(m.SchedulingFact.id))) == [row["id"]]
    h.command("coordinator", "bulk.apply", restored["undo"]["payload"])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.SchedulingFact, row["id"]).status == "declined"


def test_fact_restore_refuses_another_source_proposal_and_keeps_the_decline(h):
    payload = fact_payload(h, "person.workload")
    row = h.command("coordinator", "person.workload.propose", payload)["fact"]
    declined = transition(h, "coordinator", "person.workload.decline", row)["fact"]
    h.command("coordinator", "person.workload.propose", payload)
    refused = transition(h, "coordinator", "person.workload.restore_proposal", declined, status=409)
    assert refused["error"]["code"] == "PROPOSAL_WAITING"
    wrong = transition(h, "coordinator", "person.overtime.restore_proposal", declined, status=422)
    assert wrong["error"]["code"] == "FACT_KIND"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.SchedulingFact, row["id"]).status == "declined"


@pytest.mark.parametrize("kind", ["policy.activate", "policy.activate_many"])
@pytest.mark.parametrize("enabled", [True, False])
def test_receipt_bound_undo_restores_replaced_catalogue_rule_enabled_flag(h, kind, enabled):
    from .test_bulk import preview
    from .test_rule_catalogue_conflicts import line, most
    initial = h.command("coordinator", "rule.propose", {"catalogue_id": "duties_per_week", "on": enabled,
        "lines": [line(numbers=most(5))], **REASON})["record"]
    if not enabled:
        # Historical approved off rules retain both an off definition and active=False.
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Policy, initial["id"]).active = False
    old = transition(h, "approver", "policy.activate", initial)["record"]
    assert old["status"] == "active" and old["active"] is enabled
    newer = h.command("coordinator", "rule.propose", {"catalogue_id": "duties_per_week",
        "expected_policy_id": old["id"], "expected_revision": old["revision"],
        "lines": [line(numbers=most(4))], **REASON})["record"]
    payload = {"replaces_ids": [old["id"]], **REASON}
    item = {"target_id": newer["id"], "expected_revision": newer["revision"], "payload": payload}
    if kind == "policy.activate_many":
        item = {"payload": {**payload, "items": [{"policy_id": newer["id"], "expected_revision": newer["revision"]}]}}
    applied = h.command("approver", "bulk.apply", {"item_kind": kind, "items": [item], **REASON})
    undo = applied["undo"]["payload"]
    checked = preview(h, "approver", undo)
    restored_preview = next(row for row in checked["items"] if row["target_id"] == old["id"])
    assert restored_preview["after"].get("active", False) is enabled
    h.command("approver", "bulk.apply", undo)
    with transaction(h.app.state.engine) as db:
        restored = db.get(m.Policy, old["id"])
        assert restored.status == "active" and restored.active is enabled
        assert restored.definition == old["definition"]
        assert db.get(m.Policy, newer["id"]).status == "retired"


@pytest.mark.parametrize("enabled", [False, True])
def test_ordinary_policy_restore_keeps_the_approved_switch(h, enabled):
    original = transition(h, "approver", "policy.activate", proposal(h, active=enabled))["record"]
    retired = transition(h, "approver", "policy.retire", original)["record"]
    transition(h, "approver", "policy.restore", retired, status=422, payload={**REASON, "active": False})
    restored = transition(h, "approver", "policy.restore", retired)["record"]
    assert restored["status"] == "active" and restored["active"] is enabled


def test_bulk_duplicate_pending_fact_source_is_atomic(h):
    payload = fact_payload(h, "person.workload")
    refused = h.command("coordinator", "bulk.apply", {"item_kind": "person.workload.propose",
        "items": [{"payload": payload}, {"payload": payload}], **REASON}, status=409)
    assert refused["error"]["code"] == "PROPOSAL_WAITING"
    assert refused["error"]["details"]["failed_index"] == 1
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.SchedulingFact.id))) == []
        assert list(db.scalars(select(m.DefinitionHistory.id).where(m.DefinitionHistory.kind == "scheduling_fact"))) == []


def test_bulk_fact_decline_then_same_source_proposal_uses_current_status(h):
    payload = fact_payload(h, "person.workload")
    original = h.command("coordinator", "person.workload.propose", payload)["fact"]
    applied = h.command("coordinator", "bulk.apply", {"items": [
        {"item_kind": "person.workload.decline", "target_id": original["id"], "expected_revision": original["revision"], "payload": REASON},
        {"item_kind": "person.workload.propose", "payload": payload}], **REASON})
    assert applied["count"] == 2
    with transaction(h.app.state.engine) as db:
        rows = list(db.scalars(select(m.SchedulingFact)))
        assert len(rows) == 2 and db.get(m.SchedulingFact, original["id"]).status == "declined"
        assert [row.status for row in rows if row.id != original["id"]] == ["proposed"]


def test_bulk_restored_pending_fact_source_blocks_another_proposal(h):
    payload = fact_payload(h, "person.workload")
    original = h.command("coordinator", "person.workload.propose", payload)["fact"]
    declined = transition(h, "coordinator", "person.workload.decline", original)["fact"]
    refused = h.command("coordinator", "bulk.apply", {"items": [
        {"item_kind": "person.workload.restore_proposal", "target_id": declined["id"], "expected_revision": declined["revision"], "payload": REASON},
        {"item_kind": "person.workload.propose", "payload": payload}], **REASON}, status=409)
    assert refused["error"]["code"] == "PROPOSAL_WAITING" and refused["error"]["details"]["failed_index"] == 1
    with transaction(h.app.state.engine) as db:
        assert db.get(m.SchedulingFact, original["id"]).status == "declined"


def test_bulk_decline_then_same_catalogue_proposal_uses_current_status(h):
    from .test_rule_catalogue_conflicts import line, most
    payload = {"catalogue_id": "duties_per_week", "lines": [line(numbers=most(5))], **REASON}
    original = h.command("coordinator", "rule.propose", payload)["record"]
    applied = h.command("coordinator", "bulk.apply", {"items": [
        {"item_kind": "policy.decline", "target_id": original["id"], "expected_revision": original["revision"], "payload": REASON},
        {"item_kind": "rule.propose", "payload": payload}], **REASON})
    assert applied["count"] == 2
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, original["id"]).status == "retired"
        pending = list(db.scalars(select(m.Policy).where(m.Policy.kind == "catalogue", m.Policy.status == "proposed")))
        assert len(pending) == 1 and pending[0].id != original["id"]
    h.command("coordinator", "bulk.apply", applied["undo"]["payload"])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, original["id"]).status == "proposed"
