"""Rule retirement and restoration share configurable, independent, revision-bound decisions."""
import csv
from datetime import timedelta
import io

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm.attributes import set_committed_value

from apps.api import models as m
from apps.api.db import transaction
from .clock_support import freeze_clock
from .governance_support import identity, retire_rule, retirement_vote
from .test_bulk_reverse_transitions import REASON, proposal, transition
from .test_staff_wishes import department_settings
from packages.contracts.schemas import ENDED_REASON
from packages.domain.time import aware


def settings(h, count=None, *, status=200):
    me = h.get("admin", "/api/me")
    dept, org = me["department"], me["organisation"]
    return h.command("admin", "department.settings", {
        "department_name": dept["name"], "department_revision": dept["revision"],
        "organisation_name": org["name"], "timezone": org["timezone"], "week_start": org["week_start"],
        **({"policy_retire_approvals_required": count} if count is not None else {}), **REASON},
        dept["id"], org["revision"], status=status)


def active(h):
    return transition(h, "approver", "policy.activate", proposal(h))["record"]


def bulk(h, role, kind, row):
    return h.command(role, "bulk.apply", {"item_kind": kind, "items": [
        {"target_id": row["id"], "expected_revision": row["revision"], "payload": REASON}], **REASON})


def test_retirement_approval_setting_defaults_validates_preserves_and_undoes(h):
    original = h.get("admin", "/api/me")
    assert original["department"]["policy_retire_approvals_required"] == 1
    for invalid in (0, 4, True, 1.5, "2"):
        assert settings(h, invalid, status=422)["error"]["code"] == "SCHEMA_INVALID"
    saved = settings(h, 2)
    assert saved["department"]["policy_retire_approvals_required"] == 2
    assert saved["previous"]["policy_retire_approvals_required"] == 1
    assert saved["organisation"] == original["organisation"]
    assert settings(h)["department"] == saved["department"]
    restored = h.command("admin", "department.settings", {**saved["previous"], **REASON},
        original["department"]["id"], original["organisation"]["revision"])
    assert restored["department"]["policy_retire_approvals_required"] == 1
    with transaction(h.app.state.engine) as db:
        versions = list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == "department_settings").order_by(m.DefinitionHistory.object_revision)))
        assert [row.snapshot["policy_retire_approvals_required"] for row in versions] == [2, 1]


def test_approval_count_and_animation_settings_save_reload_and_undo_together(h):
    before = h.get("admin", "/api/me")
    original = before["department"]
    chosen = {"policy_retire_approvals_required": 2,
              "autoschedule_animation": {**original["autoschedule_animation"], "mode": "off", "speed": 1.5}}
    saved = h.command("admin", "department.settings", {**department_settings(h), **chosen},
        original["id"], before["organisation"]["revision"])
    assert {key: saved["department"][key] for key in chosen} == chosen
    assert {key: saved["previous"][key] for key in chosen} == {key: original[key] for key in chosen}
    reloaded = h.get("coordinator", "/api/me")["department"]
    assert {key: reloaded[key] for key in chosen} == chosen
    omitted = h.command("admin", "department.settings", department_settings(h),
        original["id"], saved["organisation"]["revision"])
    assert omitted["department"] == saved["department"]
    restored = h.command("admin", "department.settings", {**saved["previous"], "reason": "Undo both department settings"},
        original["id"], saved["organisation"]["revision"])
    assert {key: restored["department"][key] for key in chosen} == {key: original[key] for key in chosen}
    assert restored["organisation"]["input_revision"] == before["organisation"]["input_revision"]
    reloaded = h.get("coordinator", "/api/me")["department"]
    assert {key: reloaded[key] for key in chosen} == {key: original[key] for key in chosen}


@pytest.mark.parametrize("backend", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_two_approver_lifecycle_receipts_preserve_original_activation_evidence_and_labels(request, backend, monkeypatch):
    h = request.getfixturevalue(backend)
    row = active(h)
    original = next(policy for policy in h.get("approver", "/api/policies?status=active")["policies"]
                    if policy["id"] == row["id"])
    assert original["approved_at"] is not None and original["approved_by_name"]
    H = m.DefinitionHistory
    activation = select(H.id, H.actor_id, H.created_at, H.reason, H.snapshot).where(
        H.kind == "policy", H.object_id == row["id"], H.object_revision == row["revision"])
    with transaction(h.app.state.engine) as db:
        original_evidence = dict(db.execute(activation).mappings().one())
    assert original_evidence["snapshot"]["approval_transition"] is True
    settings(h, 2)
    second = identity(h, "second_approver", ["approver"])
    labels = ("approved_at", "approved_by", "approved_by_name", "reason")
    for hours, (kind, status) in enumerate((("policy.retire", "retired"), ("policy.restore", "active")), start=1):
        moment = aware(original["approved_at"]) + timedelta(hours=hours)
        freeze_clock(monkeypatch, moment)
        pending = transition(h, "approver", kind, row)
        assert not pending["approval"]["complete"] and pending["record"] == row
        unchanged = next(policy for policy in h.get("approver", "/api/policies?status=" + row["status"])["policies"]
                         if policy["id"] == row["id"])
        assert {key: unchanged[key] for key in labels} == {key: original[key] for key in labels}
        completed = transition(h, "second_approver", kind, row)
        assert completed["approval"]["complete"]
        row = completed["record"]
        current = next(policy for policy in h.get("approver", "/api/policies?status=" + status)["policies"]
                       if policy["id"] == row["id"])
        assert {key: current[key] for key in labels} == {key: original[key] for key in labels}
        with transaction(h.app.state.engine) as db:
            assert dict(db.execute(activation).mappings().one()) == original_evidence
    with transaction(h.app.state.engine) as db:
        receipts = list(db.execute(select(H.snapshot, H.actor_id).where(
            H.kind == "policy_transition_approval", H.object_id == row["id"]).order_by(H.object_revision)))
        assert [(snapshot["action"], actor) for snapshot, actor in receipts] == [
            (action, person) for action in ("retire", "restore") for person in (h.seed["identities"]["approver"], second)]


def test_bulk_settings_and_undo_preserve_the_independent_rule_approval_count(h):
    settings(h, 3)
    initial = h.get("admin", "/api/bulk/settings")
    changed = h.command("admin", "department.bulk_settings", {**initial["settings"], "bulk_direct_max": 12, **REASON},
        h.seed["department_id"], initial["department_revision"])
    assert changed["department"]["policy_retire_approvals_required"] == 3
    undo = changed["undo"]
    h.command("admin", undo["kind"], undo["payload"], undo["target_id"], undo["expected_revision"])
    assert h.get("admin", "/api/me")["department"]["policy_retire_approvals_required"] == 3


def test_one_approval_requires_independence_or_audited_self_approval_for_safety_rule(h):
    row = active(h)
    h.command("admin", "role.grant", {"identity_id": row["created_by"], "role": "approver",
        "starts_on": "2020-01-01", **REASON})
    refused = transition(h, "coordinator", "policy.retire", row, status=403)
    assert refused["error"]["code"] == "SELF_APPROVAL"
    permission = h.command("admin", "self_approval.grant", {"identity_id": row["created_by"],
        "actions": ["rule.activate"], "starts_on": "2026-01-01",
        "reason": "Temporary sole approver coverage"})["permission"]
    for payload in ({"reason": "Routine update"}, REASON):
        refused = transition(h, "coordinator", "policy.retire", row, payload=payload, status=403)
        assert refused["error"]["code"] == "SELF_APPROVAL"
    events = h.get("auditor", f"/api/audit?target_id={row['id']}")["events"]
    assert all("self_approved" not in event["detail"] for event in events)
    with transaction(h.app.state.engine) as db:
        current = db.get(m.Policy, row["id"])
        assert (current.status, current.active, current.revision) == ("active", True, row["revision"])
        assert not list(db.scalars(select(m.Outbox).where(m.Outbox.kind == "self_approval")))

    pending = transition(h, "approver", "policy.retire", row)
    assert pending["record"] == row and pending["approval"]["approvals_required"] == 2
    assert not pending["approval"]["complete"]
    retired = retirement_vote(h, row, REASON["reason"])
    assert retired["approval"]["complete"] and retired["record"]["active"] is False
    restored = transition(h, "coordinator", "policy.restore", retired["record"],
                          payload={"reason": "Routine update"}, status=422)
    assert restored["error"]["code"] == "SELF_APPROVAL_REASON"
    restored = transition(h, "coordinator", "policy.restore", retired["record"])
    assert restored["record"]["active"] is True and "approval" not in restored

    preference = transition(h, "approver", "policy.activate", proposal(h, name="Weekly duty preference",
        definition={"severity": "advisory", "rule": {"kind": "max_duties", "maximum": 5}}))["record"]
    refused = transition(h, "coordinator", "policy.retire", preference,
                         payload={"reason": "Routine update"}, status=422)
    assert refused["error"]["code"] == "SELF_APPROVAL_REASON"
    retired = transition(h, "coordinator", "policy.retire", preference)
    assert "approval" not in retired and retired["record"]["active"] is False

    for target, action in ((row, "policy.restore"), (preference, "policy.retire")):
        [event] = h.get("auditor", f"/api/audit?target_id={target['id']}&self_approved=true")["events"]
        assert event["action"] == action
        assert event["detail"]["self_approved"] == [{"action": "rule.activate", "permission_id": permission["id"]}]
    with transaction(h.app.state.engine) as db:
        notices = list(db.scalars(select(m.Outbox.payload).where(m.Outbox.kind == "self_approval")))
        assert len(notices) == 2
        assert {notice["target_id"] for notice in notices} == {row["id"], preference["id"]}


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("vehicle", ["individual", "bulk"])
def test_retirement_restoration_and_undo_respect_the_approval_count(h, count, vehicle):
    row = active(h)
    settings(h, count)
    identity(h, "second_approver", ["approver"])

    def check_audit(reply, kind, decision):
        events = h.get("auditor", "/api/audit?target_id=" + row["id"])["events"]
        [event] = [event for event in events if event["detail"].get("receipt_id") == reply["receipt_id"]]
        approval = decision.get("approval")
        assert event["action"] == kind + ("_pending" if approval and not approval["complete"] else "")
        assert event["detail"].get("approval") == approval

    for kind, target_status in (("policy.retire", "retired"), ("policy.restore", "active")):
        before = row
        if vehicle == "bulk":
            reply = bulk(h, "approver", kind, row)
            decision = reply["items"][0]["results"][0]
        else:
            reply = decision = transition(h, "approver", kind, row)
        check_audit(reply, kind, decision)
        if count == 2 or kind == "policy.retire":
            assert decision["record"] == before
            assert decision["approval"]["approvals_received"] == 1 and not decision["approval"]["complete"]
            if vehicle == "bulk":
                assert reply["items"][0]["result"] == "waiting" and reply["undo"] is None
                reply = bulk(h, "second_approver", kind, row)
                decision = reply["items"][0]["results"][0]
            else:
                reply = decision = transition(h, "second_approver", kind, row)
            check_audit(reply, kind, decision)
            assert decision["approval"]["complete"] and decision["approval"]["approvals_received"] == 2
        else:
            assert "approval" not in decision
        row = decision["record"]
        assert row["status"] == target_status and row["revision"] == before["revision"] + 1
        assert row["active"] is (target_status == "active")
        if vehicle == "bulk":
            assert reply["undo"] is not None
    # Undo the restoration uses the same count, and its first pending call cannot retire the rule.
    if vehicle == "bulk":
        undone = h.command("approver", **reply["undo"])
        check_audit(undone, "policy.retire", undone["items"][0]["results"][0])
        assert undone["items"][0]["result"] == "waiting" and undone["undo"] is None
        undone = h.command("second_approver", **reply["undo"])
        check_audit(undone, "policy.retire", undone["items"][0]["results"][0])
        assert undone["items"][0]["record"]["status"] == "retired"
    with transaction(h.app.state.engine) as db:
        approvals = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == "policy_transition_approval",
            m.DefinitionHistory.object_id == row["id"])))
        assert len(approvals) == 2 + 2 * (count == 2) + 2 * (vehicle == "bulk")
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.target_id == row["id"],
            m.AuditEvent.action.in_(["policy.retire", "policy.restore", "policy.retire_pending", "policy.restore_pending"]))))
        actions = {"policy.retire", "policy.restore", "policy.retire_pending"}
        if count == 2:
            actions.add("policy.restore_pending")
        assert {event.action for event in events} == actions
        assert all(event.action.endswith("_pending") is not event.detail["approval"]["complete"]
                   for event in events if "approval" in event.detail)
    exported = h.client("auditor").get("/api/audit.csv", params={"target_id": row["id"]})
    assert exported.status_code == 200, exported.text
    actions = [entry["action"] for entry in csv.DictReader(io.StringIO(exported.text))
               if entry["action"].startswith(("policy.retire", "policy.restore"))]
    assert sorted(actions) == sorted(event.action for event in events)


@pytest.mark.parametrize("kind", ["policy.retire", "policy.restore"])
def test_multiple_approvals_exclude_rule_proposer_aliases_and_duplicate_people_even_with_permission(h, kind):
    row = active(h)
    if kind == "policy.restore":
        row = retire_rule(h, row, REASON["reason"])["record"]
    settings(h, 2)
    author_person, reviewer_person = h.seed["people"][:2]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["coordinator"]).person_id = author_person
        db.get(m.Identity, h.seed["identities"]["approver"]).person_id = reviewer_person
    author = identity(h, "author_approver", ["approver"], person=author_person)
    identity(h, "reviewer_alias", ["approver"], person=reviewer_person)
    identity(h, "independent_approver", ["approver"])
    h.command("admin", "self_approval.grant", {"identity_id": author, "actions": ["rule.activate"],
        "starts_on": "2020-01-01", "reason": "Synthetic permission cannot waive independent retirement"})
    assert transition(h, "author_approver", kind, row, status=403)["error"]["code"] == "SELF_APPROVAL"
    first = transition(h, "approver", kind, row)
    assert not first["approval"]["complete"]
    for role in ("approver", "reviewer_alias"):
        assert transition(h, role, kind, row, status=409)["error"]["code"] == "DUPLICATE_REVIEWER"
    assert transition(h, "independent_approver", kind, row)["approval"]["complete"]


def test_pending_approval_cannot_be_reused_after_revision_changes(h):
    row = active(h)
    settings(h, 2)
    identity(h, "second_approver", ["approver"])
    assert not transition(h, "approver", "policy.retire", row)["approval"]["complete"]
    settings(h, 1)
    retired = transition(h, "second_approver", "policy.retire", row)["record"]
    restored = transition(h, "approver", "policy.restore", retired)["record"]
    settings(h, 2)
    pending = transition(h, "second_approver", "policy.retire", restored)
    assert pending["record"] == restored and pending["approval"]["approvals_received"] == 1
    assert transition(h, "approver", "policy.retire", restored)["approval"]["complete"]


def test_disabled_approver_does_not_fill_a_pending_approval_seat(h):
    row = active(h)
    settings(h, 2)
    identity(h, "second_approver", ["approver"])
    identity(h, "third_approver", ["approver"])
    transition(h, "approver", "policy.retire", row)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["approver"]).disabled = True
    second = transition(h, "second_approver", "policy.retire", row)
    assert second["record"] == row and second["approval"]["approvals_received"] == 1
    assert transition(h, "third_approver", "policy.retire", row)["approval"]["complete"]


def test_multi_approver_undo_rejects_unauthorised_people_and_changed_payload(h):
    row = active(h)
    identity(h, "second_approver", ["approver"])
    assert bulk(h, "second_approver", "policy.retire", row)["undo"] is None
    source = bulk(h, "approver", "policy.retire", row)
    undo = source["undo"]
    # The existing owner-only Undo contract is unchanged with the default approval count.
    assert h.command("second_approver", **undo, status=403)["error"]["code"] == "FORBIDDEN"
    settings(h, 2)
    assert h.command("coordinator", **undo, status=403)["error"]["code"] == "FORBIDDEN"
    tampered = {**undo["payload"], "reason": "Replace the recorded Undo reason"}
    assert h.command("second_approver", undo["kind"], tampered, status=409)["error"]["code"] == "BULK_UNDO_CHANGED"
    pending = h.command("second_approver", **undo)
    assert pending["items"][0]["result"] == "waiting" and pending["undo"] is None
    complete = h.command("approver", **undo)
    assert complete["items"][0]["record"]["status"] == "active"


def test_grouped_retirement_undo_collects_one_independent_decision_before_dependent_inverses(h):
    original = active(h)
    grouped = transition(h, "approver", "policy.activate", proposal(h,
        definition={**original["definition"], "merged_ids": [original["id"]]}))["record"]
    identity(h, "second_approver", ["approver"])
    assert bulk(h, "approver", "policy.retire", grouped)["undo"] is None
    retired = bulk(h, "second_approver", "policy.retire", grouped)
    undo = retired["undo"]
    assert len(undo["payload"]["items"]) == 2
    pending = h.command("second_approver", **undo)
    assert all(row["result"] == "waiting" for row in pending["items"]) and pending["undo"] is None
    complete = h.command("approver", **undo)
    assert all(row["result"] == "changed" for row in complete["items"])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, original["id"]).status == "retired"
        assert db.get(m.Policy, grouped["id"]).status == "active"


def test_partial_retirement_approval_does_not_authorise_different_group_restoration_effects(h):
    original = active(h)
    grouped = transition(h, "approver", "policy.activate", proposal(h,
        definition={**original["definition"], "merged_ids": [original["id"]]}))["record"]
    settings(h, 2)
    identity(h, "second_approver", ["approver"])
    ended = transition(h, "approver", "policy.retire", grouped, payload={"reason": ENDED_REASON + ": synthetic end"})
    assert not ended["approval"]["complete"]
    pending = transition(h, "second_approver", "policy.retire", grouped)
    assert pending["approval"]["approvals_received"] == 1 and pending["record"] == grouped
    complete = transition(h, "approver", "policy.retire", grouped)
    assert complete["approval"]["complete"] and complete["restored_ids"] == [original["id"]]


@pytest.mark.parametrize("statement", [
    "UPDATE definition_history SET reason='rewritten' WHERE id=:id",
    "DELETE FROM definition_history WHERE id=:id",
])
def test_policy_transition_decisions_are_immutable_evidence(h, statement):
    row = active(h)
    settings(h, 2)
    transition(h, "approver", "policy.retire", row)
    with transaction(h.app.state.engine) as db:
        approval_id = db.scalar(select(m.DefinitionHistory.id).where(m.DefinitionHistory.kind == "policy_transition_approval"))
    with pytest.raises(DBAPIError, match="immutable evidence"), transaction(h.app.state.engine, write=True) as db:
        db.execute(text(statement), {"id": approval_id})


@pytest.mark.parametrize("known_creator", [True, False])
def test_default_retirement_requires_two_people_even_without_creator(h, monkeypatch, known_creator):
    from apps.api.services import policies

    row = active(h)
    reviewer_person = h.seed["people"][1]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["approver"]).person_id = reviewer_person
    identity(h, "reviewer_alias", ["approver"], person=reviewer_person)
    identity(h, "second_approver", ["approver"])
    if not known_creator:
        scoped = policies.scoped

        def without_creator(db, model, target, actor):
            policy = scoped(db, model, target, actor)
            if model is m.Policy and target == row["id"]:
                set_committed_value(policy, "created_by", None)
            return policy

        monkeypatch.setattr(policies, "scoped", without_creator)

    first = transition(h, "approver", "policy.retire", row)
    assert first["record"]["active"] and first["record"]["revision"] == row["revision"]
    assert (first["approval"]["approvals_received"], first["approval"]["approvals_required"]) == (1, 2)
    duplicate = transition(h, "reviewer_alias", "policy.retire", row, status=409)
    assert duplicate["error"]["code"] == "DUPLICATE_REVIEWER"
    second = transition(h, "second_approver", "policy.retire", row)
    assert second["record"]["status"] == "retired" and second["approval"]["complete"]
    assert set(second["approval"]["approved_by"]) == {h.seed["identities"][name]
        for name in ("approver", "second_approver")}


@pytest.mark.parametrize("case", ["weaker", "alias", "legacy", "equal", "other_scope", "replacement"])
def test_retire_then_propose_cannot_self_approve_a_weaker_rule(h, monkeypatch, case):
    from apps.api.services import policies

    freeze_clock(monkeypatch, aware("2026-10-04T00:00:00+03:00"))
    original = active(h)
    person = h.seed["people"][1]
    proposer = identity(h, "new_author", ["coordinator", "approver"], person=person)
    h.command("admin", "self_approval.grant", {"identity_id": proposer, "actions": ["rule.activate"],
        "starts_on": "2026-10-01", "reason": "Temporary review cover"})
    if case == "replacement":
        other = proposal(h, definition={**original["definition"], "replaces_id": original["id"]})
        transition(h, "new_author", "policy.activate", other)
    else:
        signer = "new_author"
        if case == "alias":
            signer = "author_alias"
            identity(h, signer, ["approver"], person=person)
        if case == "legacy":
            # Simulate a historical retirement with audit evidence but no signer list in its definition history.
            history = policies.history

            def legacy_history(db, actor, kind, row, reason, **kwargs):
                if kind == "policy" and row.status == "retired":
                    kwargs.pop("snapshot_extra", None)
                return history(db, actor, kind, row, reason, **kwargs)

            monkeypatch.setattr(policies, "history", legacy_history)
            transition(h, "approver", "policy.retire", original)
            retired = transition(h, signer, "policy.retire", original)
        else:
            transition(h, signer, "policy.retire", original)
            retired = transition(h, "approver", "policy.retire", original)
        assert retired["record"]["status"] == "retired"

    definition = {**original["definition"],
                  "rule": {"kind": "min_rest", "hours": 10 if case == "equal" else 8}}
    if case == "other_scope":
        definition["scope"] = {"person_id": person}
    proposed = h.command("new_author", "policy.create", {
        **{key: original[key] for key in ("name", "owner", "source", "starts_on")}, "definition": definition})["record"]
    weaker = case in {"weaker", "alias", "legacy"}
    reviewed = h.client("approver").post(f"/api/policies/{proposed['id']}/review", json={})
    assert reviewed.status_code == 200 and reviewed.json()["weakens_safety"] is weaker
    attempted = transition(h, "new_author", "policy.activate", proposed, status=403 if weaker else 200)
    if weaker:
        assert attempted["error"]["code"] == "SELF_APPROVAL"
        assert "weakens a safety rule" in attempted["error"]["message"]
        attempted = transition(h, "approver", "policy.activate", proposed)
    assert attempted["record"]["status"] == "active"


def test_safety_activation_undo_waits_before_restoring_the_rule_it_replaced(h):
    original = active(h)
    pending = proposal(h, definition={**original["definition"], "replaces_id": original["id"]})
    activated = transition(h, "approver", "policy.activate", pending)
    replacement = activated["record"]
    undo = activated["undo"]
    assert undo["kind"] == "bulk.apply"
    assert [item["item_kind"] for item in undo["payload"]["items"]] == ["policy.retire", "policy.restore"]
    identity(h, "second_approver", ["approver"])
    waiting = h.command("approver", **undo)
    assert [item["result"] for item in waiting["items"]] == ["waiting", "waiting"]
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, replacement["id"]).status == "active"
        assert db.get(m.Policy, original["id"]).status == "retired"
    completed = h.command("second_approver", **undo)
    assert [item["result"] for item in completed["items"]] == ["changed", "changed"]
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, replacement["id"]).status == "retired"
        assert db.get(m.Policy, original["id"]).status == "active"
