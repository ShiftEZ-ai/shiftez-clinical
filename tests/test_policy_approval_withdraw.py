"""RUL-03 Undo of a retire or restore approval still waiting for other approvers: the approver's own approval is
withdrawn (nothing was applied), recorded and audited; it no longer counts towards the quorum."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
from threading import Event

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from starlette.requests import Request

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services import policies
from packages.contracts import schemas as s
from packages.contracts.audit import AUDIT_ACTION_CATALOGUE
from .governance_support import identity, retire_rule
from .test_bulk_reverse_transitions import REASON, proposal, transition
from .test_policy_transition_approvals import settings


def withdraw(h, role, row, status=200):
    return h.command(role, "policy.withdraw_approval", REASON, row["id"], row["revision"], status=status)


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def approval_harness(request):
    return request.getfixturevalue(request.param)


def waiting(h, action="retire"):
    row = transition(h, "approver", "policy.activate", proposal(h))["record"]
    if action == "restore":
        row = retire_rule(h, row, REASON["reason"])["record"]
    settings(h, 2)
    identity(h, "second_approver", ["approver"])
    assert not transition(h, "approver", "policy." + action, row)["approval"]["complete"]
    return row


@pytest.mark.parametrize("action", ["retire", "restore"])
def test_withdrawn_approval_no_longer_counts_and_can_be_given_again(approval_harness, action):
    h = approval_harness
    row = waiting(h, action)
    assert withdraw(h, "approver", row)["withdrawn"] == action
    assert withdraw(h, "approver", row, status=409)["error"]["code"] == "WORKFLOW_STATE"
    assert withdraw(h, "coordinator", row, status=403)
    # The second approver's vote alone is one of two: the withdrawn one is not counted.
    pending = transition(h, "second_approver", "policy." + action, row)["approval"]
    assert (pending["approvals_received"], pending["complete"]) == (1, False)
    done = transition(h, "approver", "policy." + action, row)
    assert done["approval"]["complete"] and done["record"]["status"] == ("retired" if action == "retire" else "active")
    # Applied: no vote waits at the new revision, so there is nothing to withdraw.
    assert withdraw(h, "approver", done["record"], status=409)["error"]["code"] == "WORKFLOW_STATE"
    events = h.get("auditor", "/api/audit?target_id=" + row["id"])["events"]
    assert "policy.withdraw_approval" in {event["action"] for event in events}


def test_only_current_approver_can_withdraw_the_exact_signing_accounts_vote(approval_harness):
    h = approval_harness
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["approver"]).person_id = h.seed["people"][0]
    identity(h, "approver_alias", ["approver"], person=h.seed["people"][0])
    row = waiting(h)
    for role in set(s.ROLE_NAMES) - {"admin", "approver"}:
        if role not in h.seed["identities"]:
            identity(h, role, [role])
        assert withdraw(h, role, row, status=403)["error"]["code"] == "FORBIDDEN"
    for role in ("second_approver", "approver_alias", "admin"):
        assert withdraw(h, role, row, status=409)["error"]["code"] == "WORKFLOW_STATE"
    with transaction(h.app.state.engine, write=True) as db:
        for binding in db.scalars(select(m.RoleBinding).where(
                m.RoleBinding.identity_id == h.seed["identities"]["approver"])):
            binding.revoked = True
    assert withdraw(h, "approver", row, status=403)["error"]["code"] == "FORBIDDEN"


def test_foreign_department_and_proposer_cannot_withdraw_or_reuse_someone_elses_vote(approval_harness):
    h = approval_harness
    row = waiting(h)
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=db.get(m.Department, h.seed["department_id"]).organisation_id,
                                  name="Other synthetic department")
        db.add(department)
        db.flush()
        foreign_id = department.id
    identity(h, "foreign_approver", ["approver"], department=foreign_id)
    assert withdraw(h, "foreign_approver", row, status=404)["error"]["code"] == "NOT_FOUND"
    h.command("admin", "role.grant", {"identity_id": row["created_by"], "role": "approver",
        "starts_on": "2020-01-01", **REASON})
    h.command("admin", "self_approval.grant", {"identity_id": row["created_by"], "actions": ["rule.activate"],
        "starts_on": "2020-01-01", **REASON})
    assert withdraw(h, "coordinator", row, status=409)["error"]["code"] == "WORKFLOW_STATE"
    withdraw(h, "approver", row)
    assert transition(h, "coordinator", "policy.retire", row, status=403)["error"]["code"] == "SELF_APPROVAL"
    assert transition(h, "second_approver", "policy.retire", row)["approval"]["approvals_received"] == 1


def test_withdrawal_binds_current_hash_without_resurrecting_older_effects_votes(approval_harness):
    h = approval_harness
    original = transition(h, "approver", "policy.activate", proposal(h))["record"]
    grouped = transition(h, "approver", "policy.activate", proposal(h,
        definition={**original["definition"], "merged_ids": [original["id"]]}))["record"]
    settings(h, 2)
    identity(h, "second_approver", ["approver"])
    reasons = [REASON, {"reason": s.ENDED_REASON + ": synthetic end"}]
    for payload in reasons:
        assert transition(h, "approver", "policy.retire", grouped, payload=payload)["approval"]["approvals_received"] == 1
    withdraw(h, "approver", grouped)
    with transaction(h.app.state.engine) as db:
        votes = list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == "policy_transition_approval", m.DefinitionHistory.object_id == grouped["id"])
            .order_by(m.DefinitionHistory.object_revision)))
        assert len(votes) == 3
        assert votes[0].snapshot["decision_hash"] != votes[1].snapshot["decision_hash"]
        assert votes[2].snapshot == {**votes[1].snapshot, "withdrawn": True}
    for payload in reasons:
        pending = transition(h, "second_approver", "policy.retire", grouped, payload=payload)
        assert pending["record"] == grouped and pending["approval"]["approvals_received"] == 1
    pending = transition(h, "approver", "policy.retire", grouped)
    assert pending["record"] == grouped and pending["approval"]["approvals_received"] == 1
    done = transition(h, "second_approver", "policy.retire", grouped)
    assert done["approval"]["complete"] and done["restored_ids"] == [original["id"]]


def test_initial_activation_retains_independent_review_and_has_no_pending_vote_to_withdraw(approval_harness):
    h = approval_harness
    row = proposal(h)
    settings(h, 2)
    h.command("admin", "role.grant", {"identity_id": row["created_by"], "role": "approver",
        "starts_on": "2020-01-01", **REASON})
    assert transition(h, "coordinator", "policy.activate", row, status=403)["error"]["code"] == "SELF_APPROVAL"
    activated = transition(h, "approver", "policy.activate", row)["record"]
    assert activated["status"] == "active" and activated["approved_by"] == h.seed["identities"]["approver"]
    assert withdraw(h, "approver", activated, status=409)["error"]["code"] == "WORKFLOW_STATE"


def test_withdrawal_appends_immutable_history_and_catalogued_audit_only(approval_harness):
    h = approval_harness
    row = waiting(h)

    def evidence():
        with transaction(h.app.state.engine) as db:
            return {cls: {item.id: record(item) for item in db.scalars(select(cls).where(
                (cls.object_id if cls is m.DefinitionHistory else cls.target_id) == row["id"]))}
                for cls in (m.DefinitionHistory, m.AuditEvent)}

    before = evidence()
    reply = withdraw(h, "approver", row)
    assert reply["record"] == row
    after = evidence()
    for cls in before:
        assert {key: after[cls][key] for key in before[cls]} == before[cls]
        added = list(after[cls].keys() - before[cls].keys())
        assert len(added) == 1
        item = after[cls][added[0]]
        if cls is m.DefinitionHistory:
            assert item["kind"] == "policy_transition_approval" and item["snapshot"]["withdrawn"] is True
            assert item["actor_id"] == h.seed["identities"]["approver"] and item["reason"] == REASON["reason"]
        else:
            assert item["action"] == "policy.withdraw_approval"
            assert item["detail"]["receipt_id"] == reply["receipt_id"]
            assert item["action"] in AUDIT_ACTION_CATALOGUE
        for operation in ("UPDATE " + cls.__tablename__ + " SET reason='rewritten'", "DELETE FROM " + cls.__tablename__):
            with pytest.raises(DBAPIError, match="immutable evidence"), transaction(h.app.state.engine, write=True) as db:
                db.execute(text(operation + " WHERE id=:id"), {"id": item["id"]})
    assert withdraw(h, "approver", row, status=409)["error"]["code"] == "WORKFLOW_STATE"
    assert evidence() == after


@pytest.mark.parametrize("action", ["retire", "restore"])
def test_applied_change_refuses_withdrawal_at_both_old_and_current_revision(approval_harness, action):
    h = approval_harness
    row = waiting(h, action)
    applied = transition(h, "second_approver", "policy." + action, row)["record"]
    assert withdraw(h, "approver", row, status=409)["error"]["code"] == "STALE_REVISION"
    assert withdraw(h, "approver", applied, status=409)["error"]["code"] == "WORKFLOW_STATE"


@pytest.mark.postgres
@pytest.mark.parametrize("action", ["retire", "restore"])
@pytest.mark.parametrize("first", ["withdraw", "approve"])
def test_postgres_history_conflict_rolls_back_racing_withdrawal_or_approval(pg, monkeypatch, action, first):
    """Overlap service reads without the command bus lock; the history uniqueness token must still protect apply."""
    h, held, release = pg, Event(), Event()
    prepared = {operation: Event() for operation in ("withdraw", "approve")}
    row = waiting(h, action)
    engine = h.app.state.engine
    second_pid = []
    replay = policies.transition_approval_state

    def concurrent_replay(db, actor, policy, **kwargs):
        state = replay(db, actor, policy, **kwargs)
        operation = "withdraw" if actor.identity_id == h.seed["identities"]["approver"] else "approve"
        prepared[operation].set()
        assert all(ready.wait(15) for ready in prepared.values()), "Both operations must read the same history token"
        if operation != first:
            assert held.wait(15), "First operation did not write its history token"
        return state

    monkeypatch.setattr(policies, "transition_approval_state", concurrent_replay)

    def execute(operation):
        role = "approver" if operation == "withdraw" else "second_approver"
        kind = "policy.withdraw_approval" if operation == "withdraw" else "policy." + action
        try:
            with transaction(engine) as db:
                if operation != first:
                    second_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
                actor = Actor(h.seed["identities"][role], h.seed["department_id"],
                    db.get(m.Department, h.seed["department_id"]).organisation_id, role, frozenset({"approver"}), None, "test")
                command = s.Command(kind=kind, target_id=row["id"], expected_revision=row["revision"], payload=REASON)
                result = (policies.withdraw_transition_approval if operation == "withdraw" else policies.policy_transition)(
                    db, actor, command)
                db.flush()
                if operation == first:
                    held.set()
                    assert release.wait(15), "Race was not released"
                return result
        except IntegrityError as exc:
            assert exc.orig.sqlstate == "23505"
            assert "definition_history" in exc.orig.diag.constraint_name
            request = Request({"type": "http", "state": {"correlation_id": "synthetic-race"}})
            return asyncio.run(h.app.exception_handlers[IntegrityError](request, exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            leading = pool.submit(execute, first)
            trailing = pool.submit(execute, "approve" if first == "withdraw" else "withdraw")
            assert held.wait(15), "First operation did not hold its transaction"
            with engine.connect() as observer:
                for _ in range(500):
                    if observer.scalar(text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": second_pid[0]}):
                        break
                    release.wait(0.01)
                else:
                    pytest.fail("Second operation did not block on the unique history token")
            assert not trailing.done()
        finally:
            release.set()
        first_result, second_result = leading.result(timeout=15), trailing.result(timeout=15)
    monkeypatch.setattr(policies, "transition_approval_state", replay)
    assert second_result.status_code == 409
    error = json.loads(second_result.body)["error"]
    assert error["code"] == "INTEGRITY_CONFLICT" and "no partial change" in error["message"]
    with transaction(engine) as db:
        stored = db.get(m.Policy, row["id"])
        votes = list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == "policy_transition_approval", m.DefinitionHistory.object_id == row["id"])
            .order_by(m.DefinitionHistory.object_revision)))
        if action == "restore":
            assert [vote.snapshot["action"] for vote in votes[:2]] == ["retire", "retire"]
        assert [vote.object_revision for vote in votes] == ([1, 2, 3, 4] if action == "restore" else [1, 2])
        assert [vote.snapshot["action"] for vote in votes[-2:]] == [action, action]
    if first == "withdraw":
        assert first_result["withdrawn"] == action
        assert record(stored) == row and votes[-1].snapshot["withdrawn"] is True
        retried = transition(h, "second_approver", "policy." + action, row)
        assert retried["record"] == row
        assert (retried["approval"]["approvals_received"], retried["approval"]["complete"]) == (1, False)
        assert transition(h, "approver", "policy." + action, row)["approval"]["complete"]
    else:
        assert first_result["approval"]["approvals_received"] == 2 and first_result["approval"]["complete"]
        assert stored.revision == row["revision"] + 1 and all(not vote.snapshot.get("withdrawn") for vote in votes)
        assert withdraw(h, "approver", row, status=409)["error"]["code"] == "STALE_REVISION"
        assert withdraw(h, "approver", record(stored), status=409)["error"]["code"] == "WORKFLOW_STATE"
