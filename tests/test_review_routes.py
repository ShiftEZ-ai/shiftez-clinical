from .clock_support import freeze_clock
from datetime import UTC, datetime, timedelta
import copy
import uuid
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity, route


def submitted(h):
    return h.lifecycle(h.shift(h.workspace())["workspace"], "submit")["workspace"]


def test_effective_route_requires_all_sequential_reviewers(h):
    identity(h, "programme", ["programme_lead"])
    route(h, [{"key": "programme", "label": "Programme review", "role": "programme_lead"},
              {"key": "clinical", "label": "Clinical review", "role": "approver"}])
    w = submitted(h)
    assert h.lifecycle(w, "approve", "approver", status=403)["error"]["code"] == "REVIEW_STEP_ROLE"
    w = h.lifecycle(w, "approve", "programme")["workspace"]
    assert w["status"] == "submitted"
    progress = h.get("programme", "/api/workspaces/" + w["id"])["review_progress"]
    assert progress["steps"][0]["complete"] and progress["pending_step"]["key"] == "clinical"
    assert h.lifecycle(w, "publish", status=409)["error"]["code"] == "WORKFLOW_STATE"
    w = h.lifecycle(w, "approve", "approver")["workspace"]
    assert w["status"] == "approved"
    pub = h.lifecycle(w, "publish")
    assert pub["sequence"] == 1
    assert "review_snapshot" not in h.get("staff", "/api/publications/" + pub["publication_id"])["snapshot"]["workspace"]


def test_quorum_requires_distinct_reviewers_and_preserves_idempotent_receipts(h):
    identity(h, "reviewer_two", ["approver"])
    route(h, [{"key": "clinical", "label": "Two reviewers", "approvals_required": 2}])
    w = submitted(h)
    key = str(uuid.uuid4()); payload = {"reason": "First independent review"}
    result = h.command("approver", "workspace.approve", payload, w["id"], w["revision"], key=key)
    assert h.command("approver", "workspace.approve", payload, w["id"], w["revision"], key=key) == result
    w = result["workspace"]
    assert w["status"] == "submitted"
    assert h.lifecycle(w, "approve", "approver", status=409)["error"]["code"] == "DUPLICATE_REVIEWER"
    w = h.lifecycle(w, "approve", "reviewer_two")["workspace"]
    assert h.lifecycle(w, "publish")["publication_id"]


def test_one_person_cannot_fill_two_seats_using_other_accounts(h):
    person = h.seed["people"][2]
    identity(h, "review_a", ["approver"], person)
    identity(h, "review_b", ["approver"], person)
    route(h, [{"key": "clinical", "label": "Two people", "approvals_required": 2}])
    w = h.lifecycle(submitted(h), "approve", "review_a")["workspace"]
    assert h.lifecycle(w, "approve", "review_b", status=409)["error"]["code"] == "DUPLICATE_REVIEWER"


def test_every_candidate_contributor_is_excluded_not_only_last_editor(h):
    identity(h, "middle_editor", ["coordinator", "approver"])
    w = h.shift(h.workspace())["workspace"]
    shift_id = h.detail(w)["shifts"][0]["id"]
    w = h.command("middle_editor", "shift.action", {"workspace_id": w["id"], "shift_ids": [shift_id],
        "action": "lock", "reason": "Middle contributor change"}, revision=w["revision"])["workspace"]
    w = h.command("coordinator", "shift.action", {"workspace_id": w["id"], "shift_ids": [shift_id],
        "action": "unlock", "reason": "Later contributor change"}, revision=w["revision"])["workspace"]
    w = h.lifecycle(w, "submit")["workspace"]
    assert h.lifecycle(w, "approve", "middle_editor", status=403)["error"]["code"] == "SELF_APPROVAL"


def test_self_approval_via_another_account_bound_to_same_person_is_blocked(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["coordinator"]).person_id = h.seed["people"][2]
    identity(h, "editor_other_account", ["approver"], h.seed["people"][2])
    w = submitted(h)
    assert h.lifecycle(w, "approve", "editor_other_account", status=403)["error"]["code"] == "SELF_APPROVAL"


def test_deadline_opens_only_configured_escalation_role_and_never_autoapproves(h):
    route(h, [{"key": "programme", "label": "Escalatable review", "role": "programme_lead",
               "deadline_hours": 1, "escalation_role": "approver"}])
    w = submitted(h)
    assert h.lifecycle(w, "approve", "approver", status=403)["error"]["code"] == "REVIEW_STEP_ROLE"
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, w["id"]).submitted_at = m.now() - timedelta(hours=2)
    progress = h.detail(w)["review_progress"]
    assert progress["pending_step"]["escalated"] and not progress["complete"]
    w = h.lifecycle(w, "approve", "approver")["workspace"]
    assert h.lifecycle(w, "publish")["publication_id"]


def test_missing_escalation_never_grants_approver_programme_authority(h):
    route(h, [{"key": "programme", "label": "No escalation", "role": "programme_lead", "deadline_hours": 1}])
    w = submitted(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, w["id"]).submitted_at = m.now() - timedelta(days=5)
    assert not h.detail(w)["review_progress"]["pending_step"]["escalated"]
    h.lifecycle(w, "approve", "approver", status=403)


def delegation(h, row, delegate="approver", delegator="programme", step="programme"):
    return h.command(delegator, "review_delegation.create", {"route_id": row["id"], "step_key": step,
        "delegate_id": h.seed["identities"][delegate], "starts_at": (m.now() - timedelta(minutes=1)).isoformat(),
        "expires_at": (m.now() + timedelta(days=1)).isoformat(), "reason": "Bounded independent delegation"})["delegation"]


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_home_review_card_follows_delegation_start_and_expiry(h, monkeypatch):
    clock = datetime(2026, 10, 4, 8, tzinfo=UTC)
    freeze_clock(monkeypatch, lambda: clock)
    identity(h, 'programme', ['programme_lead'])
    r = route(h, [{'key': 'programme', 'label': 'Programme review', 'role': 'programme_lead'}])
    w = submitted(h)
    start, expiry = clock + timedelta(minutes=10), clock + timedelta(minutes=20)
    h.command('programme', 'review_delegation.create', {
        'route_id': r['id'], 'step_key': 'programme', 'delegate_id': h.seed['identities']['approver'],
        'starts_at': start.isoformat(), 'expires_at': expiry.isoformat(), 'reason': 'Bounded independent delegation',
    })

    def home_review():
        home = h.get('approver', '/api/home')
        return [item['rota_id'] for item in home['attention'] if item['key'] == 'reviews'], home.get('review_refresh_in_ms')

    assert home_review() == ([], 600_000)
    clock += timedelta(seconds=1)
    assert home_review() == ([], 599_000)
    clock = start
    assert home_review() == ([w['id']], 600_000)
    clock = expiry
    assert home_review() == ([], None)


def test_home_refresh_uses_another_delegation_only_for_an_actionable_review(h):
    identity(h, 'programme', ['programme_lead'])
    identity(h, 'next_approver', ['approver'])
    identity(h, 'other_programme', ['programme_lead'])
    review = route(h, [{'key': 'programme', 'label': 'Programme review', 'role': 'programme_lead'},
                       {'key': 'clinical', 'label': 'Clinical review', 'role': 'approver'}])
    grant = delegation(h, review)
    rota = submitted(h)
    h.command('approver', 'workspace.approve', {'reason': 'Delegated review', 'delegation_id': grant['id']},
              rota['id'], rota['revision'])

    actionable = h.get('next_approver', '/api/home')
    assert any(item['key'] == 'reviews' and item['rota_id'] == rota['id'] for item in actionable['attention'])
    assert actionable['review_refresh_in_ms'] > 0
    unrelated = h.get('other_programme', '/api/home')
    assert not any(item['key'] == 'reviews' for item in unrelated['attention'])
    assert 'review_refresh_in_ms' not in unrelated


def test_scoped_delegation_is_real_authority_and_remains_revocable(h):
    identity(h, "programme", ["programme_lead"])
    r = route(h, [{"key": "programme", "label": "Programme review", "role": "programme_lead"}])
    d = delegation(h, r)
    w = submitted(h)
    w = h.command("approver", "workspace.approve", {"reason": "Review under bounded delegation", "delegation_id": d["id"]},
                  w["id"], w["revision"])["workspace"]
    assert w["status"] == "approved"
    h.command("programme", "review_delegation.revoke", {"reason": "Delegation ended"}, d["id"], d["revision"])
    assert h.lifecycle(w, "publish", status=409)["error"]["code"] == "DELEGATION_INACTIVE"
    assert not h.get("coordinator", "/api/publications")["publications"]


def test_delegated_review_can_publish_while_both_authorities_are_active(h):
    identity(h, "programme", ["programme_lead"])
    r = route(h, [{"key": "programme", "label": "Programme review", "role": "programme_lead"}])
    d = delegation(h, r); w = submitted(h)
    w = h.command("approver", "workspace.approve", {"reason": "Delegated review", "delegation_id": d["id"]},
                  w["id"], w["revision"])["workspace"]
    assert h.lifecycle(w, "publish")["publication_id"]


@pytest.mark.parametrize("change,code", [("expired", "DELEGATION_INACTIVE"), ("delegator_revoked", "APPROVER_REVOKED"),
                                         ("delegate_revoked", "APPROVER_REVOKED")])
def test_delegation_rechecked_at_publication(h, monkeypatch, change, code):
    identity(h, "programme", ["programme_lead"])
    r = route(h, [{"key": "programme", "label": "Programme review", "role": "programme_lead"}])
    d = delegation(h, r); w = submitted(h)
    w = h.command("approver", "workspace.approve", {"reason": "Delegated review", "delegation_id": d["id"]},
                  w["id"], w["revision"])["workspace"]
    with transaction(h.app.state.engine, write=True) as db:
        if change == "expired":
            expired_now = m.now() + timedelta(days=1, seconds=5)
            freeze_clock(monkeypatch, lambda: expired_now)
            h.clients.pop('coordinator').close()
        else:
            username = "programme" if change == "delegator_revoked" else "approver"
            db.get(m.Identity, h.seed["identities"][username]).disabled = True
    assert h.lifecycle(w, "publish", status=409)["error"]["code"] == code


def test_delegation_cannot_target_unqualified_staff_or_another_step(h):
    identity(h, "programme", ["programme_lead"])
    r = route(h, [{"key": "programme", "label": "Programme review", "role": "programme_lead"},
                  {"key": "clinical", "label": "Clinical review", "role": "approver"}])
    payload = {"route_id": r["id"], "step_key": "programme", "delegate_id": h.seed["identities"]["staff"],
               "starts_at": m.now().isoformat(), "expires_at": (m.now()+timedelta(days=1)).isoformat(), "reason": "Cannot delegate to staff"}
    assert h.command("programme", "review_delegation.create", payload, status=403)["error"]["code"] == "DELEGATE_INELIGIBLE"
    assert h.command("approver", "review_delegation.create", payload, status=403)["error"]["code"] == "DELEGATION_AUTHORITY"
    h.command("programme", "review_delegation.create", {**payload, "expires_at": (m.now()+timedelta(days=15)).isoformat()}, status=422)


def test_route_activation_overlap_and_effective_boundary_fail_closed(h):
    r = route(h, start="2026-10-06")
    w = h.shift(h.workspace())["workspace"]
    assert h.lifecycle(w, "submit", status=409)["error"]["code"] == "REVIEW_ROUTE_BOUNDARY"
    other = route(h, activate=False)
    assert h.command("approver", "review_route.activate", {"reason": "Overlapping route"}, other["id"], other["revision"], status=409)["error"]["code"] == "ROUTE_OVERLAP"
    h.command("approver", "review_route.retire", {"reason": "Explicit retirement"}, r["id"], r["revision"])
    assert h.lifecycle(w, "submit", status=409)["error"]["code"] == "REVIEW_ROUTE_REQUIRED"


def test_route_retirement_invalidates_submitted_route_instead_of_using_default(h):
    r = route(h); w = submitted(h)
    h.command("approver", "review_route.retire", {"reason": "No implicit fallback"}, r["id"], r["revision"])
    assert h.lifecycle(w, "approve", "approver", status=409)["error"]["code"] == "REVIEW_ROUTE_REQUIRED"
    w = h.lifecycle(w, "reopen")["workspace"]
    assert h.lifecycle(w, "submit", status=409)["error"]["code"] == "REVIEW_ROUTE_REQUIRED"


def test_urgent_amendment_selects_approved_route_not_a_validation_bypass(h):
    identity(h, "programme", ["programme_lead"])
    pub = h.publish(h.shift(h.workspace())["workspace"])
    route(h, [{"key": "programme", "label": "Routine programme", "role": "programme_lead"}],
          urgent=[{"key": "urgent_clinical", "label": "Urgent independent clinical", "role": "approver"}])
    w = h.command("coordinator", "workspace.create", {"name": "Urgent amendment", "starts_on": "2026-10-04", "ends_on": "2026-10-11",
        "base_publication_id": pub["publication_id"], "urgency": "urgent", "urgency_reason": "Unexpected staffing change"})["workspace"]
    # D11 retains issued checks for untouched sessions; this test needs an actual changed session.
    from .test_published_operational import update_duty
    shift = h.detail(w)['shifts'][0]
    w = update_duty(h, w, shift, participants=[{'person_id': pid, 'start': shift['start'], 'end': shift['end']}
                                             for pid in h.seed['people'][:2]])
    w = h.lifecycle(w, "submit")["workspace"]
    assert w["review_snapshot"]["steps"][0]["key"] == "urgent_clinical"
    w = h.lifecycle(w, "approve", "approver")["workspace"]
    h.command("coordinator", "availability.record", {"person_id": h.seed["people"][0], "start": "2026-10-04T08:00:00+03:00",
        "end": "2026-10-04T12:00:00+03:00", "kind": "actual_absence", "private_reason": "Private fixture"})
    # Urgent publication also reports mandatory validation before stale approval.
    refused = h.lifecycle(w, "publish", status=422)["error"]
    assert refused["code"] == "VALIDATION_FAILED"
    assert refused["details"]["stale_approval"]["code"] == "STALE_APPROVAL"
    w = h.lifecycle(w, "reopen")["workspace"]
    assert h.lifecycle(w, "submit", status=422)["error"]["code"] == "VALIDATION_FAILED"
    assert h.get("coordinator", "/api/publications")["publications"][0]["id"] == pub["publication_id"]


def test_urgent_without_explicit_route_is_refused(h):
    pub = h.publish(h.shift(h.workspace())["workspace"])
    payload = {"name": "Urgent", "starts_on": "2026-10-04", "ends_on": "2026-10-11", "base_publication_id": pub["publication_id"],
               "urgency": "urgent", "urgency_reason": "Urgent candidate reason"}
    assert h.command("coordinator", "workspace.create", payload, status=409)["error"]["code"] == "URGENT_ROUTE_UNCONFIGURED"
    h.command("coordinator", "workspace.create", {**payload, "base_publication_id": None}, status=422)


def test_review_evidence_is_append_only_at_database_boundary(h):
    w = h.lifecycle(submitted(h), "approve", "approver")["workspace"]
    for statement in ("UPDATE review_decisions SET reason='changed'", "DELETE FROM review_decisions"):
        with pytest.raises(IntegrityError), transaction(h.app.state.engine, write=True) as db:
            db.execute(text(statement))
    assert h.detail(w)["review_progress"]["complete"]


@pytest.mark.parametrize("payload", [
    {"steps": []}, {"steps": [{"key": "x", "label": "X", "role": "admin"}]},
    {"steps": [{"key": "x", "label": "X", "role": "approver", "escalation_role": "approver"}]},
    {"steps": [{"key": "x", "label": "X", "approvals_required": 0}]},
    {"steps": [{"key": "x", "label": "X"}], "urgent_steps": [{"key": "x", "label": "X"}]},
    {"schema_version": 2, "steps": [{"key": "x", "label": "X"}]},
])
def test_unsupported_route_semantics_never_saved(h, payload):
    h.command("admin", "review_route.create", {"name": "Invalid route", "starts_on": "2026-10-01", "definition": payload,
        "reason": "Constructed invalid configuration"}, status=422)
    assert not h.get("admin", "/api/review-routes")["routes"]


def test_a_self_approval_permission_never_passes_through_a_delegation(h):
    # The delegator built the rota; their delegate holds a permission. The delegate may not approve it on their behalf.
    identity(h, "programme", ["programme_lead", "coordinator"])
    r = route(h, [{"key": "programme", "label": "Programme review", "role": "programme_lead"}])
    d = delegation(h, r)
    h.command("admin", "self_approval.grant", {"role": "approver", "actions": ["rota.approve"], "starts_on": "2026-01-01", "ends_on": "2027-01-01",
              "reason": "Short of approvers"})
    w = h.command("programme", "workspace.create", {"name": "Delegator built", "starts_on": "2026-10-04", "ends_on": "2026-10-11"})["workspace"]
    w = h.shift(w)["workspace"]
    w = h.command("programme", "workspace.submit", {"reason": "Ready"}, w["id"], w["revision"])["workspace"]
    refused = h.command("approver", "workspace.approve", {"reason": "Delegated review", "delegation_id": d["id"]},
                        w["id"], w["revision"], status=403)
    assert refused["error"]["code"] == "SELF_APPROVAL"
