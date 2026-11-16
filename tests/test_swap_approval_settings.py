"""Department-controlled automation keeps staff consent separate from independent approval."""
from .clock_support import freeze_clock
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from apps.api import models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import planning, reviews, swaps
from apps.worker.main import process_one
from packages.contracts.schemas import AssignmentSource
from packages.domain.time import aware
from .governance_support import identity, route, transition
from .test_staff_swaps import fixed_clock, offered, source_pair


def settings(h, **changes):
    me = h.get("admin", "/api/me")
    org, dept = me["organisation"], me["department"]
    return h.command("admin", "department.settings", {
        "department_name": dept["name"], "department_revision": dept["revision"],
        "organisation_name": org["name"], "timezone": org["timezone"], "week_start": org["week_start"],
        "reason": "Configure synthetic swap approval", **changes}, dept["id"], org["revision"])


def agree(h, *, key=None, kind="exchange"):
    pid, first, second, source = source_pair(h)
    row = offered(h, pid, first, kind=kind)
    row = transition(h, row, "accept", "staff_two")["swap"] if kind == "give" else transition(
        h, row, "counteroffer", "staff_two", {"assignment_id": second, "reason": "Offer this exact duty"})["swap"]
    result = transition(h, row, "confirm", key=key)
    assert "workspace" not in result and "validation" not in result
    return result, source


def swap_route(h, steps=None, **values):
    row = h.command("admin", "review_route.create", {
        "name": "Dedicated swap approval", "starts_on": "2020-01-01", "purpose": "swap",
        "definition": {"steps": steps or [{"key": "swap", "label": "Swap approval", "role": "approver"}]},
        "reason": "Propose independent swap review", **values})["route"]
    return h.command("approver", "review_route.activate", {"reason": "Independent route activation"},
                     row["id"], row["revision"])["route"]


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("kind", ["exchange", "give"])
def test_default_automation_submits_as_system_and_publishes_after_review(request, database, kind):
    h = request.getfixturevalue(database)
    result, source = agree(h, kind=kind)
    row = result["swap"]
    prepared = h.detail(row["workspace_id"])
    workspace = prepared["workspace"]
    assert row["status"] == "reviewing" and row["prepare_refusal"] is None
    assert workspace["status"] == "submitted" and workspace["created_by"] not in h.seed["identities"].values()
    assert workspace["review_snapshot"]["route_id"] is None
    for index, shift in enumerate(prepared["shifts"]):
        participant = shift["participants"][0]
        if kind == "give" and index:
            assert {key: participant[key] for key in AssignmentSource.model_fields} == {
                key: source["shifts"][index]["participants"][0][key] for key in AssignmentSource.model_fields}
        else:
            assert participant["source"] == "swap" and participant["source_run_id"] is None
            assert participant["source_actor_id"] == workspace["created_by"]
            assert aware(participant["source_at"]) == m.now()
    with transaction(h.app.state.engine) as db:
        actor = db.get(m.Identity, workspace["created_by"])
        assert actor.disabled and actor.person_id is None and actor.password_hash is None
        assert not db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == actor.id))
        assert db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == "workspace.submit",
                                                   m.AuditEvent.actor_id == actor.id)).detail["automatic"]
    approved = h.lifecycle(workspace, "approve", "approver")
    assert approved["workspace"]["status"] == "published"
    final = h.get("staff", "/api/swaps/" + row["id"])
    assert final["swap"]["status"] == "published"
    assert [e["action"] for e in final["events"]] == [
        "offered", "accept" if kind == "give" else "counteroffer", "confirm", "prepare", "published"]
    assert final["events"][-1]["actor_id"] == actor.id
    with transaction(h.app.state.engine) as db:
        publication = db.get(m.Publication, approved["publication_id"])
        assert publication.published_by == actor.id
        assert db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == "workspace.publish",
                                                   m.AuditEvent.actor_id == actor.id)).detail["swap_id"] == row["id"]
    assert h.get("coordinator", "/api/publications/" + row["publication_id"])["snapshot"] == source


@pytest.mark.parametrize("auto_prepare,auto_publish", [(False, False), (False, True), (True, False)])
def test_automation_switches_are_independent_and_ordinary_rotas_stay_manual(h, auto_prepare, auto_publish):
    settings(h, swap_auto_prepare=auto_prepare, swap_auto_publish=auto_publish)
    result, _ = agree(h)
    row = result["swap"]
    if not auto_prepare:
        assert row["status"] == "accepted" and row["workspace_id"] is None
        result = transition(h, row, "prepare", "coordinator")
        assert result["workspace"]["status"] == "draft"
        result = h.lifecycle(result["workspace"], "submit")
    workspace = h.detail(row["workspace_id"])["workspace"] if auto_prepare else result["workspace"]
    result = h.lifecycle(workspace, "approve", "approver")
    assert result["workspace"]["status"] == ("published" if auto_publish else "approved")
    if not auto_publish:
        assert h.lifecycle(result["workspace"], "publish")["workspace"]["status"] == "published"
    ordinary = h.shift(h.workspace(start="2026-10-11", end="2026-10-18"),
                       start="2026-10-11T08:00:00+03:00", end="2026-10-11T12:00:00+03:00")["workspace"]
    assert h.lifecycle(h.lifecycle(ordinary, "submit")["workspace"], "approve", "approver")["workspace"]["status"] == "approved"


def test_selected_route_is_swap_only_and_waits_for_all_distinct_reviewers(h):
    routine = route(h)
    dedicated = swap_route(h, [
        {"key": "clinical", "label": "Clinical", "role": "approver"},
        {"key": "programme", "label": "Programme", "role": "programme_lead", "approvals_required": 2}])
    settings(h, swap_review_route_id=dedicated["id"])
    identity(h, "lead_one", ["programme_lead"])
    identity(h, "lead_two", ["programme_lead"])
    result, _ = agree(h)
    w = h.detail(result["swap"]["workspace_id"])["workspace"]
    assert w["review_snapshot"]["route_id"] == dedicated["id"]
    w = h.lifecycle(w, "approve", "approver")["workspace"]
    assert w["status"] == "submitted"
    w = h.lifecycle(w, "approve", "lead_one")["workspace"]
    assert w["status"] == "submitted"
    assert h.lifecycle(w, "approve", "lead_one", status=409)["error"]["code"] == "DUPLICATE_REVIEWER"
    assert h.lifecycle(w, "approve", "lead_two")["workspace"]["status"] == "published"
    source_workspace = h.get("coordinator", "/api/publications/" + result["swap"]["publication_id"])["snapshot"]["workspace"]
    assert source_workspace["review_snapshot"]["route_id"] == routine["id"]


@pytest.mark.parametrize("refusal", ["validation", "route"])
@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_preparation_refusals_preserve_consent_and_leave_no_half_amendment(request, database, refusal, monkeypatch):
    h = request.getfixturevalue(database)
    if refusal == "route":
        dedicated = swap_route(h, ends_on="2026-10-06")
        settings(h, swap_review_route_id=dedicated["id"])
    pid, first, second, source = source_pair(h)
    row = transition(h, offered(h, pid, first), "counteroffer", "staff_two",
                     {"assignment_id": second, "reason": "Offer a duty"})["swap"]
    if refusal == "validation":
        prepare = swaps.prepare
        def absence_after_eligibility(db, actor, row, value):
            # Change real inputs only after confirmation's eligibility check; the savepoint must roll them back.
            assignment = db.get(m.Assignment, row.first_assignment_id)
            db.add(m.Availability(department_id=row.department_id, person_id=row.recipient_person_id,
                start=assignment.start, end=assignment.end, kind="actual_absence",
                private_reason="Private clinical detail must never leak", recorded_by=actor.identity_id))
            db.flush()
            return prepare(db, actor, row, value)
        monkeypatch.setattr(swaps, "prepare", absence_after_eligibility)
    models = (m.Workspace, m.Assignment, m.ChangeSet, m.Availability)
    with transaction(h.app.state.engine) as db:
        before = [db.scalar(select(func.count()).select_from(model)) for model in models]
    row = transition(h, row, "confirm")["swap"]
    assert row["status"] == "accepted" and row["workspace_id"] is None
    assert row["counteroffered_at"] is not None and row["confirmed_at"] is not None
    assert row["prepare_refusal"]["code"] == ("SWAP_INVALID" if refusal == "validation" else "REVIEW_ROUTE_BOUNDARY")
    assert "Private clinical" not in str(h.get("staff", "/api/swaps/" + row["id"]))
    with transaction(h.app.state.engine) as db:
        assert [db.scalar(select(func.count()).select_from(model)) for model in models] == before
    assert h.get("coordinator", "/api/publications/" + pid)["snapshot"] == source
    for _ in range(30):
        if not process_one(h.app.state.engine):
            break
    assert any(n["title"] == "Swap needs coordinator attention"
               for n in h.get("coordinator", "/api/notifications")["notifications"])
    assert "Private clinical" not in str(h.get("coordinator", "/api/notifications"))
    if refusal == "route":
        settings(h, swap_review_route_id=None)
        prepared = transition(h, row, "prepare", "coordinator")
        assert prepared["swap"]["prepare_refusal"] is None
        assert prepared["workspace"]["status"] == "draft"
    else:
        reopened = transition(h, row, "undo")["swap"]
        assert reopened["status"] == "counteroffered"


@pytest.mark.parametrize("change,code", [("withdraw", "SWAP_WITHDRAWN"), ("expiry", "SWAP_EXPIRED"),
                                       ("access", "SWAP_ACCESS_REVOKED"), ("route", "STALE_REVIEW_ROUTE")])
def test_automatic_publication_keeps_current_consent_and_route_checks(h, monkeypatch, change, code):
    dedicated = swap_route(h)
    settings(h, swap_review_route_id=dedicated["id"])
    result, _ = agree(h)
    if change == "withdraw":
        transition(h, result["swap"], "withdraw", "staff_two")
    elif change == "expiry":
        later = m.now() + timedelta(days=1, seconds=1)
        freeze_clock(monkeypatch, lambda: later)
    elif change == "access":
        h.command("admin", "identity.disable", {"reason": "Revoke consent authority"}, h.seed["identities"]["staff_two"], 1)
    else:
        settings(h, swap_review_route_id=None)
    w = h.detail(result["swap"]["workspace_id"])["workspace"]
    assert h.lifecycle(w, "approve", "approver", status=409)["error"]["code"] == code
    assert len(h.get("coordinator", "/api/publications")["publications"]) == 1


@pytest.mark.parametrize("alias", [False, True])
def test_swap_parties_cannot_use_self_approval_permissions(h, alias):
    result, _ = agree(h)
    who = "staff_two"
    if alias:
        who = "recipient_alias"
        identity(h, who, ["approver"], h.seed["people"][1])
    else:
        h.command("admin", "role.grant", {"identity_id": h.seed["identities"][who], "role": "approver",
                  "starts_on": "2020-01-01", "reason": "Synthetic dual role"})
    h.command("admin", "self_approval.grant", {"identity_id": h.seed["identities"][who], "actions": ["rota.approve"],
              "starts_on": "2026-01-01", "ends_on": "2027-01-01", "reason": "Synthetic permission cannot override swap independence"})
    w = h.detail(result["swap"]["workspace_id"])["workspace"]
    assert h.lifecycle(w, "approve", who, status=403)["error"]["code"] == "SELF_APPROVAL"


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_confirmation_receipt_does_not_prepare_twice(request, database):
    h = request.getfixturevalue(database)
    h.policy({"kind": "block_quota", "starts_on": "2026-10-01", "ends_on": "2026-11-01",
              "metric": "duties", "comparison": "maximum", "amount": 3})
    pid, first, second, source = source_pair(h, third=True)
    row = transition(h, offered(h, pid, first), "counteroffer", "staff_two",
                     {"assignment_id": second, "reason": "Offer this exact duty"})["swap"]
    key = str(uuid.uuid4())
    command = {"kind": "swap.confirm", "target_id": row["id"], "expected_revision": row["revision"],
               "payload": {"reason": "Explicit synthetic consent decision"}}
    # Use raw HTTP JSON: Harness.command enriches planning replies by reading their checks as coordinator.
    def confirm():
        response = h.client("staff").post("/api/commands", headers={"idempotency-key": key}, json=command)
        assert response.status_code == 200, response.text
        return response.json()
    result = confirm()
    assert confirm() == result
    assert h.get("staff", "/api/commands/" + key) == {"status": "committed", "result": result}
    for who in ("staff_two", "staff_three"):
        assert h.get(who, "/api/commands/" + key, status=404)["error"]["code"] == "RECEIPT_NOT_FOUND"
    assert result["swap"]["status"] == "reviewing"
    workspace = h.detail(result["swap"]["workspace_id"])["workspace"]
    assert workspace["status"] == "submitted" and workspace["validation"]["scope"] == "published_changes"
    assert {r["person_id"] for r in workspace["validation"]["quota_report"]["rows"]} == set(h.seed["people"])
    h.get("staff", "/api/workspaces/" + workspace["id"], status=403)
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.CommandReceipt, result["receipt_id"]).result
        assert stored == result
        assert "workspace" not in stored and "validation" not in stored
    assert len(h.get("coordinator", "/api/workspaces")["workspaces"]) == 2
    assert h.get("coordinator", "/api/publications/" + pid)["snapshot"] == source
    # Full findings/quota leakage was unproven: the serializer already strips them before persistence.
    assert not {"findings", "coverage", "quota_report"} & set(result.get("validation") or {})
    assert set(result) == {"swap", "receipt_id", "activity"}
    assert h.seed["people"][2] not in str(result)


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_system_actor_creation_recovers_from_a_raced_unique_insert(request, database, monkeypatch):
    h = request.getfixturevalue(database)
    with transaction(h.app.state.engine, write=True) as db:
        winner = swaps.system_actor(db, h.seed["department_id"])
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        department.name = "Synthetic outer transaction survives"
        scalar = db.scalar
        first_read = True
        def raced_read(query, *args, **kwargs):
            nonlocal first_read
            if first_read:
                first_read = False
                return None  # A concurrent creator was not visible to the initial lookup.
            return scalar(query, *args, **kwargs)
        monkeypatch.setattr(db, "scalar", raced_read)
        actor = swaps.system_actor(db, department.id)
        assert actor == winner
        assert db.scalar(select(func.count()).select_from(m.Identity).where(
            m.Identity.issuer == swaps.SWAP_AUTOMATION_ISSUER)) == 1
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Department, h.seed["department_id"]).name == "Synthetic outer transaction survives"


@pytest.mark.parametrize("action", ["submit", "approve", "publish"])
def test_expired_swap_can_be_checked_but_cannot_advance(h, monkeypatch, action):
    settings(h, swap_auto_prepare=False, swap_auto_publish=False)
    result, _ = agree(h)
    w = transition(h, result["swap"], "prepare", "coordinator")["workspace"]
    if action in {"approve", "publish"}:
        w = h.lifecycle(w, "submit")["workspace"]
    if action == "publish":
        w = h.lifecycle(w, "approve", "approver")["workspace"]
    expired = m.now() + timedelta(days=1, seconds=1)
    freeze_clock(monkeypatch, lambda: expired)
    checked = h.lifecycle(w, "validate")["validation"]
    assert checked["valid"] and "score" not in checked and checked["scope"] == "published_changes"
    assert len(checked["coverage"]) == 2
    refused = h.lifecycle(w, action, "approver" if action == "approve" else "coordinator", status=409)
    assert refused["error"]["code"] == "SWAP_EXPIRED"
    assert len(h.get("coordinator", "/api/publications")["publications"]) == 1


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("automatic", [False, True])
def test_swap_checks_only_changed_days_and_people_but_keeps_full_publication(request, database, monkeypatch, automatic):
    h = request.getfixturevalue(database)
    settings(h, swap_auto_prepare=automatic, swap_auto_publish=automatic)
    calls = []
    original = planning.evaluate
    def checked(data, **options):
        calls.append((set(data.get("person_rule_subjects", [])), set(data["focus_ids"]), len(data["shifts"])))
        return original(data, **options)
    monkeypatch.setattr(planning, "evaluate", checked)
    typ = h.configure("assignment_types", {"name": "Unrelated synthetic duty", "starts_on": "2020-01-01",
                                          "definition": {}})["record"]
    h.policy({"kind": "minimum_cover", "minimum": 2}, {"assignment_type_id": typ["id"]}, severity="advisory")
    pid, first, second, _ = source_pair(h)
    # An ordinary amendment adds an unrelated day to the publication before consent starts.
    w = h.workspace(base=pid)
    w = h.shift(w, "2026-10-06T08:00:00+03:00", "2026-10-06T12:00:00+03:00",
                people=[h.seed["people"][2]], typ=typ["id"])["workspace"]
    pub = h.publish(w)["publication_id"]
    source = h.get("coordinator", "/api/publications/" + pub)["snapshot"]
    shifts = source["shifts"]
    first, second = [s["participants"][0]["id"] for s in shifts[:2]]
    calls.clear()
    row = transition(h, offered(h, pub, first), "counteroffer", "staff_two",
                     {"assignment_id": second, "reason": "Exact second duty"})["swap"]
    result = transition(h, row, "confirm")
    if automatic:
        w = h.detail(result["swap"]["workspace_id"])["workspace"]
    else:
        prepared = transition(h, result["swap"], "prepare", "coordinator")
        w = prepared["workspace"]
        assert w["status"] == "draft" and prepared["validation"]["scope"] == "published_changes"
        w = h.lifecycle(w, "submit")["workspace"]
    submitted = h.detail(w)["workspace"]["validation"]
    assert submitted["scope"] == "published_changes" and "score" not in submitted
    published = h.lifecycle(w, "approve", "approver")
    if not automatic:
        assert published["workspace"]["status"] == "approved"
        assert published["validation"]["scope"] == "published_changes"
        published = h.lifecycle(published["workspace"], "publish")
    assert calls and all(people == set(h.seed["people"][:2]) and len(focus) == 2 and total == 3
                         for people, focus, total in calls)
    issued = h.get("coordinator", "/api/publications/" + published["publication_id"])
    after, check = issued["snapshot"], issued["original_validation"]
    assert len(after["shifts"]) == 3
    assert [s["participants"][0]["person_id"] for s in after["shifts"]] == [
        h.seed["people"][1], h.seed["people"][0], h.seed["people"][2]]
    with transaction(h.app.state.engine) as db:
        publication = db.get(m.Publication, published["publication_id"])
        validation = publication.validation
        stored = db.get(m.Workspace, publication.workspace_id).validation
        from packages.domain.validation import INTERNAL_CHECK_KEYS
        assert '_session_parts' in stored and not INTERNAL_CHECK_KEYS.intersection(validation)
        audit_fields = {'gap_acknowledgements', 'review_item_decisions', 'review_items'}
        assert {key: value for key, value in stored.items() if key not in INTERNAL_CHECK_KEYS} == {
            key: value for key, value in validation.items() if key not in audit_fields}
        assert all(validation[key] == [] for key in audit_fields)
    assert validation["scope"] == "published_changes" and "score" not in validation
    assert {r["shift_id"] for r in validation["coverage"]} == {s["id"] for s in after["shifts"]}
    ids = {s["id"] for s in after["shifts"]}
    assert {r["shift_id"] for r in check["coverage"]} == ids
    assert check["scope"] == "published_changes" and "score" not in check
    unrelated = after["shifts"][2]["id"]
    gaps = [f for f in check["findings"] if f["code"] == "SCOPED_COVERAGE_GAP" and f["shift_id"] == unrelated]
    assert gaps and all(f["severity"] == "warning" for f in gaps)
    schedule = h.get("coordinator", "/api/schedule/sessions?start=2026-10-04&end=2026-10-11")
    assert {r["shift_id"] for r in schedule["coverage"]} == {s["id"] for s in after["shifts"]}
    assert all(f in schedule["findings"] for f in gaps)
    assert h.get("coordinator", "/api/publications/" + pub)["snapshot"] == source


def test_automatic_notifications_report_review_without_unrelated_staff_details(h):
    result, _ = agree(h)
    for _ in range(30):
        if not process_one(h.app.state.engine):
            break
    for who in ("staff", "staff_two"):
        notes = h.get(who, "/api/notifications")["notifications"]
        assert any(n["title"] == "Swap rota change in review" for n in notes)
    assert not h.get("staff_three", "/api/notifications")["notifications"]
    assert h.detail(result["swap"]["workspace_id"])["workspace"]["status"] == "submitted"


def test_proposed_swap_route_preserves_purpose_for_older_clients_and_can_change_explicitly(h):
    row = h.command("admin", "review_route.create", {
        "name": "Swap route", "purpose": "swap", "starts_on": "2020-01-01",
        "definition": {"steps": [{"key": "swap", "label": "Swap review"}]}, "reason": "Propose route"})["route"]
    payload = {key: row[key] for key in ("name", "starts_on", "ends_on", "definition")}
    updated = h.command("admin", "review_route.update", {**payload, "name": "Renamed", "reason": "Edit older form"},
                        row["id"], row["revision"])["route"]
    assert updated["purpose"] == "swap"
    changed = h.command("admin", "review_route.update", {**payload, "purpose": "rota", "reason": "Explicitly change purpose"},
                        row["id"], updated["revision"])["route"]
    assert changed["purpose"] == "rota"


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_activated_route_purpose_cannot_change_even_during_retirement(request, database):
    h = request.getfixturevalue(database)
    row = swap_route(h)
    with pytest.raises(DBAPIError), transaction(h.app.state.engine, write=True) as db:
        db.execute(text("UPDATE review_routes SET status='retired', purpose='rota' WHERE id=:id"), {"id": row["id"]})
    assert next(r for r in h.get("admin", "/api/review-routes")["routes"] if r["id"] == row["id"])["purpose"] == "swap"


@pytest.mark.parametrize("boundary", ["offer", "prepare"])
def test_neighboring_unmodified_day_still_blocks_a_swap_for_rest(h, boundary):
    if boundary == "offer":
        h.policy({"kind": "min_rest", "hours": 24})
    else:
        settings(h, swap_auto_prepare=False)
    pid, _, _, _ = source_pair(h)
    w = h.workspace(base=pid)
    w = h.shift(w, "2026-10-06T08:00:00+03:00", "2026-10-06T12:00:00+03:00",
                people=[h.seed["people"][0]])["workspace"]
    pid = h.publish(w)["publication_id"]
    source = h.get("coordinator", "/api/publications/" + pid)["snapshot"]
    first, second = [s["participants"][0]["id"] for s in source["shifts"][:2]]
    if boundary == "offer":
        refused = h.command("staff", "swap.offer", {"publication_id": pid, "assignment_id": first,
            "recipient_person_id": h.seed["people"][1], "expires_at": (m.now()+timedelta(days=1)).isoformat(),
            "reason": "Propose duties that leave too little rest"}, status=422)
        assert refused["error"]["code"] == "SWAP_INELIGIBLE"
    else:
        row = transition(h, offered(h, pid, first), "counteroffer", "staff_two",
                         {"assignment_id": second, "reason": "Offer the return duty"})["swap"]
        row = transition(h, row, "confirm")["swap"]
        assert row["status"] == "accepted"
        h.policy({"kind": "min_rest", "hours": 24})
        refused = transition(h, row, "prepare", "coordinator", status=422)
        assert refused["error"]["code"] == "SWAP_INVALID"
    assert any(f["code"] == "MIN_REST" for f in refused["error"]["details"]["findings"])
    assert h.get("coordinator", "/api/publications/" + pid)["snapshot"] == source
    assert len(h.get("coordinator", "/api/workspaces")["workspaces"]) == 2


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("boundary", ["prepare", "submit", "publish"])
@pytest.mark.parametrize("kind,code,overnight", [("min_rest", "MIN_REST", False),
    ("forbidden_transition", "FORBIDDEN_TRANSITION", False),
    pytest.param("forbidden_transition", "FORBIDDEN_TRANSITION", True, id="overnight-partial-transition")])
def test_late_scoped_neighbor_rule_blocks_swap_validation(request, database, monkeypatch, boundary, kind, code, overnight):
    h = request.getfixturevalue(database)
    settings(h, swap_auto_prepare=False)
    identity(h, "staff_two", ["staff"], h.seed["people"][1])
    typ = h.configure("assignment_types", {"name": "Synthetic unchanged neighbor", "starts_on": "2020-01-01",
                                          "definition": {}})["record"]
    if overnight:
        w = h.shift(h.workspace(), "2026-10-04T20:00:00+03:00", "2026-10-05T08:00:00+03:00")["workspace"]
    else:
        w = h.shift(h.workspace())["workspace"]
    neighbor_day, return_day = ("06", "08") if overnight else ("05", "06")
    w = h.shift(w, f"2026-10-{neighbor_day}T08:00:00+03:00", f"2026-10-{neighbor_day}T12:00:00+03:00",
                people=[h.seed["people"][1]], typ=typ["id"])["workspace"]
    w = h.shift(w, f"2026-10-{return_day}T13:00:00+03:00", f"2026-10-{return_day}T17:00:00+03:00",
                people=[h.seed["people"][1]])["workspace"]
    pid = h.publish(w)["publication_id"]
    source = h.get("coordinator", "/api/publications/" + pid)["snapshot"]
    first, second = [source["shifts"][i]["participants"][0]["id"] for i in (0, 2)]
    row = transition(h, offered(h, pid, first), "counteroffer", "staff_two",
                     {"assignment_id": second, "reason": "Exchange exact duties, keeping the neighboring duty"})["swap"]
    row = transition(h, row, "confirm")["swap"]
    assert row["status"] == "accepted"
    if kind == "min_rest":
        rule = h.command("coordinator", "rule.propose", {"catalogue_id": "rest_between_duties", "lines": [{
            "id": m.uid(), "who": {"conditions": [{"field": "person_ids", "values": [h.seed["people"][1]]}]},
            "where": {"conditions": [{"field": "assignment_type_ids", "values": [typ["id"]]}]},
            "numbers": {"min": {"value": 24}}}], "reason": "Rest scoped to the retained duty"})["record"]
    else:
        rule = h.command("coordinator", "policy.create", {"name": "Synthetic next-day safety rule",
            "owner": "Test policy owner", "source": "Synthetic regression", "starts_on": "2020-01-01",
            "definition": {"rule": {"kind": kind, "from_type_ids": [h.seed["type"]], "to_type_ids": [typ["id"]]},
                           "scope": {"person_id": h.seed["people"][1], "assignment_type_id": typ["id"]},
                           "severity": "mandatory", "exception_allowed": False}})["record"]
    if overnight:
        snapshot = planning.domain_snapshot
        def legacy_partial(db, workspace, *args, **kwargs):
            data = snapshot(db, workspace, *args, **kwargs)
            # New bookings must be whole duties; inject legacy partial input only at the validation boundary.
            if any(p["id"] == rule["id"] and p["status"] == "active" for p in data["policies"]):
                for shift in data["shifts"]:
                    if aware(shift["start"]) == aware("2026-10-04T20:00:00+03:00"):
                        for participant in shift["participants"]:
                            participant["start"] = "2026-10-05T00:00:00+03:00"
            return data
        monkeypatch.setattr(planning, "domain_snapshot", legacy_partial)
        monkeypatch.setattr(swaps, "domain_snapshot", legacy_partial)
    if boundary != "prepare":
        result = transition(h, row, "prepare", "coordinator")
        row, w = result["swap"], result["workspace"]
        if boundary == "publish":
            w = h.lifecycle(w, "submit")["workspace"]
    if boundary == "publish":
        revalidate = planning.revalidate
        publication_findings = []
        def capture_publication(db, workspace, *args, **kwargs):
            if workspace.status == "approved":
                # Inject after the fingerprint gate, isolating final validation from stale-approval refusal.
                policy = db.get(m.Policy, rule["id"])
                policy.status, policy.approved_by = "active", h.seed['identities']['approver']
            result = revalidate(db, workspace, *args, **kwargs)
            if workspace.status == "approved":
                publication_findings.extend(result["findings"])
            return result
        monkeypatch.setattr(planning, "revalidate", capture_publication)
        approved = h.lifecycle(w, "approve", "approver")["workspace"]
        assert approved["status"] == "approved"
        final = h.get("coordinator", "/api/swaps/" + row["id"])
        assert final["swap"]["publish_refusal"]["code"] == "VALIDATION_FAILED"
        assert final["swap"]["result_publication_id"] is None
        assert any(e["action"] == "publish_refused" for e in final["events"])
        assert code in {f["code"] for f in publication_findings}
        # The refused publication rolls back its injected input too; activate it normally for the explicit recheck.
        h.command("approver", "policy.activate", {"reason": "Activate after final-check refusal"}, rule["id"], rule["revision"])
        checked = h.lifecycle(approved, "validate")["validation"]
        assert code in {f["code"] for f in checked["findings"]}
    else:
        h.command("approver", "policy.activate", {"reason": "Activate after staff consent"}, rule["id"], rule["revision"])
        if boundary == "prepare":
            refused = transition(h, row, "prepare", "coordinator", status=422)
            assert refused["error"]["code"] == "SWAP_INVALID"
            assert h.get("staff", "/api/swaps/" + row["id"])["swap"]["status"] == "accepted"
        else:
            refused = h.lifecycle(w, "submit", status=422)
            assert refused["error"]["code"] == "VALIDATION_FAILED"
        assert code in {f["code"] for f in refused["error"]["details"]["findings"]}
    if overnight:
        with transaction(h.app.state.engine) as db:
            eligibility = swaps.Eligibility(db, db.get(m.Publication, pid)).pair(first, second)
        assert not eligibility["eligible"]
        assert code in {f["code"] for f in eligibility["findings"]}
    assert len(h.get("coordinator", "/api/publications")["publications"]) == 1
    assert h.get("coordinator", "/api/publications/" + pid)["snapshot"] == source


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_swap_quota_report_is_explicitly_scoped_and_personal_reports_stay_private(request, database, monkeypatch):
    h = request.getfixturevalue(database)
    h.policy({"kind": "block_quota", "starts_on": "2026-10-01", "ends_on": "2026-11-01",
              "metric": "duties", "comparison": "maximum", "amount": 3})
    wish = h.command("staff", "wish.create", {"person_id": h.seed["people"][0], "kind": "prefer_off",
        "repeat": {"weekdays": [6, 0], "starts_on": "2026-10-01"}})["wish"]
    revalidate, captured = planning.revalidate, {}
    def capture_checked_wishes(db, workspace, *args, **kwargs):
        result = revalidate(db, workspace, *args, **kwargs)
        if workspace.status == "draft" and kwargs.get("data_out"):
            captured[workspace.id] = (result.get("scope"), kwargs["data_out"][0]["wishes"])
        return result
    monkeypatch.setattr(planning, "revalidate", capture_checked_wishes)
    third_pattern = h.command('coordinator', 'wish.create', {'person_id': h.seed['people'][2],
        'kind': 'max_days_in_row', 'limit': 1, 'first_day': '2026-10-01', 'last_day': '2026-10-31'})['wish']
    def source_with_unrelated_pattern(h):
        identity(h, 'staff_two', ['staff'], h.seed['people'][1])
        identity(h, 'staff_three', ['staff'], h.seed['people'][2])
        workspace = h.shift(h.workspace())['workspace']
        for day, person in [(5, 1), (6, 2), (7, 2)]:
            workspace = h.shift(workspace, f'2026-10-{day:02}T08:00:00+03:00',
                                f'2026-10-{day:02}T12:00:00+03:00', people=[h.seed['people'][person]])['workspace']
        publication = h.publish(workspace)['publication_id']
        source = h.get('coordinator', '/api/publications/' + publication)['snapshot']
        first, second = [shift['participants'][0]['id'] for shift in source['shifts'][:2]]
        assert next(w for w in source['wishes'] if w['id'] == third_pattern['id'])['outcome'] == 'declined'
        return publication, first, second, source
    monkeypatch.setitem(agree.__globals__, 'source_pair', source_with_unrelated_pattern)
    result, _ = agree(h)
    # The normal source has a complete three-person report; the swap checks only its two subjects.
    original = h.get("coordinator", "/api/reports/" + result["swap"]["publication_id"])["quotas"]
    assert original["complete"] and len(original["rows"]) == 3
    w = h.detail(result["swap"]["workspace_id"])["workspace"]
    scope, concrete = captured[w["id"]]
    assert scope == w["validation"]["scope"] == "published_changes"
    assert concrete == w["validation"]["wishes"]
    assert {entry['standing_wish_id'] for entry in concrete if entry.get('standing_wish_id')} == {wish['id']}
    assert any(entry['id'] == third_pattern['id'] for entry in concrete)
    issued_check = h.get('coordinator', '/api/publications/' + result['swap']['publication_id'])['original_validation']
    assert [f for f in w['validation']['findings'] if f.get('person_id') == h.seed['people'][2]] == [
        f for f in issued_check['findings'] if f.get('person_id') == h.seed['people'][2]]
    scoped = w["validation"]["quota_report"]
    assert scoped["scope"] == "published_changes" and not scoped["complete"] and scoped["context_complete"]
    assert {r["person_id"] for r in scoped["rows"]} == set(h.seed["people"])
    pub = h.lifecycle(w, "approve", "approver")["publication_id"]
    published_wishes = h.get("coordinator", "/api/publications/" + pub)["snapshot"]["wishes"]
    assert {entry["id"] for entry in published_wishes} == {entry["id"] for entry in concrete}
    report = h.get("coordinator", "/api/reports/" + pub)["quotas"]
    assert not report["complete"] and report["scope"] == "published_changes"
    assert {r["person_id"] for r in report["rows"]} == set(h.seed["people"])
    own = h.get("staff", "/api/reports/" + pub)["quotas"]
    assert {r["person_id"] for r in own["rows"]} == {h.seed["people"][0]}
    assert h.seed["people"][1] not in str(own) and h.seed["people"][2] not in str(own)
    exported = h.client("coordinator").get("/api/reports/" + pub + "?format=quotas")
    assert exported.status_code == 200 and "not a complete department report" not in exported.text


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("change,code", [("expiry", "SWAP_EXPIRED"), ("withdraw", "SWAP_WITHDRAWN"),
                                       ("access", "SWAP_ACCESS_REVOKED")])
def test_publish_refusals_keep_human_approval_and_notify_coordinators(request, database, monkeypatch, change, code):
    h = request.getfixturevalue(database)
    result, source = agree(h)
    approve = reviews.approve_step
    expired = m.now() + timedelta(days=1, seconds=1)
    def change_after_review(db, actor, workspace, value, **kwargs):
        complete = approve(db, actor, workspace, value, **kwargs)
        if change == "expiry":
            freeze_clock(monkeypatch, lambda: expired)
        elif change == "withdraw":
            row = db.get(m.SwapRequest, result["swap"]["id"])
            row.status, row.revision = "withdrawn", row.revision + 1
        else:
            db.get(m.Identity, h.seed["identities"]["staff_two"]).disabled = True
        return complete
    monkeypatch.setattr(reviews, "approve_step", change_after_review)
    w = h.detail(result["swap"]["workspace_id"])["workspace"]
    approved = h.lifecycle(w, "approve", "approver")["workspace"]
    assert approved["status"] == "approved" and approved["revision"] == w["revision"] + 1
    assert h.detail(approved)["workspace"]["status"] == "approved"
    final = h.get("coordinator", "/api/swaps/" + result["swap"]["id"])
    refusal = final["swap"]["publish_refusal"]
    assert refusal["code"] == code and refusal["message"]
    assert final["swap"]["result_publication_id"] is None
    refused, = [event for event in final["events"] if event["action"] == "publish_refused"]
    assert refused["detail"] == refusal and refused["actor_id"] == approved["created_by"]
    assert h.get("coordinator", "/api/swaps")["swaps"][0]["publish_refusal"] == refusal
    with transaction(h.app.state.engine) as db:
        for model in (m.ReviewDecision, m.Approval):
            decision = db.scalar(select(model).where(model.workspace_id == approved["id"]))
            assert decision.approved_by == h.seed["identities"]["approver"]
        assert db.scalar(select(m.AuditEvent).where(m.AuditEvent.target_id == approved["id"],
                                                   m.AuditEvent.action == "workspace.approve"))
        assert not db.scalar(select(m.AuditEvent).where(m.AuditEvent.target_id == approved["id"],
                                                       m.AuditEvent.action == "workspace.publish"))
    assert len(h.get("coordinator", "/api/publications")["publications"]) == 1
    assert h.get("coordinator", "/api/publications/" + result["swap"]["publication_id"])["snapshot"] == source
    for _ in range(30):
        if not process_one(h.app.state.engine):
            break
    notes = h.get("coordinator", "/api/notifications")["notifications"]
    assert any(n["title"] == "Swap needs coordinator attention" and "approved but could not be published" in n["message"]
               for n in notes)


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_refused_publication_rolls_back_partial_publication_and_can_be_published_later(request, database, monkeypatch):
    h = request.getfixturevalue(database)
    result, source = agree(h)
    published = swaps.published
    def refuse_after_publication(db, actor, row, publication_id, reason):
        published(db, actor, row, publication_id, reason)
        raise Problem(409, "PUBLICATION_REFUSED", "Publication could not complete")
    monkeypatch.setattr(swaps, "published", refuse_after_publication)
    w = h.detail(result["swap"]["workspace_id"])["workspace"]
    approved = h.lifecycle(w, "approve", "approver")["workspace"]
    assert approved["status"] == "approved"
    final = h.get("staff", "/api/swaps/" + result["swap"]["id"])
    assert final["swap"]["status"] == "reviewing" and final["swap"]["result_publication_id"] is None
    assert final["swap"]["publish_refusal"]["code"] == "PUBLICATION_REFUSED"
    assert {e["action"] for e in final["events"]} == {"offered", "counteroffer", "confirm", "prepare", "publish_refused"}
    assert len(h.get("coordinator", "/api/publications")["publications"]) == 1
    original = h.get("coordinator", "/api/publications/" + result["swap"]["publication_id"])
    assert original["publication"]["current"] and original["snapshot"] == source
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.ReviewDecision).where(
            m.ReviewDecision.workspace_id == approved["id"])) == 1
        assert not db.scalar(select(m.Outbox).where(m.Outbox.kind == "swap", m.Outbox.event_key.endswith(":published")))
    resubmitted = h.lifecycle(h.lifecycle(approved, "reopen")["workspace"], "submit")["workspace"]
    approved = h.lifecycle(resubmitted, "approve", "approver")["workspace"]
    assert approved["status"] == "approved"
    again = h.get("staff", "/api/swaps/" + result["swap"]["id"])
    assert again["swap"]["revision"] == final["swap"]["revision"]
    assert sum(e["action"] == "publish_refused" for e in again["events"]) == 2
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Outbox).where(
            m.Outbox.event_key.startswith("swap:" + result["swap"]["id"] + ":"),
            m.Outbox.event_key.endswith(":publish_refused"))) == 2
        assert db.scalar(select(func.count()).select_from(m.ReviewDecision).where(
            m.ReviewDecision.workspace_id == approved["id"])) == 2
    monkeypatch.setattr(swaps, "published", published)
    assert h.lifecycle(approved, "publish")["workspace"]["status"] == "published"
    assert h.get("staff", "/api/swaps/" + result["swap"]["id"])["swap"]["publish_refusal"] is None
    assert h.get("coordinator", "/api/swaps")["swaps"][0]["publish_refusal"] is None
