"""Exact approval unions, rollback accounting and normalized row conflicts."""
import pytest
import uuid
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from apps.api import commands, models as m
from apps.api.db import transaction
from apps.api.errors import INTEGRITY_CONFLICT_MESSAGE, Problem
from .test_bulk import people_change, preview
from .test_bulk_context_receipts import REASON, activation, assert_waiting, checked, scenario
from .test_bulk_single_commands import relationship


def test_bulk_preview_keeps_union_proof_failure_on_every_activation(h):
    workspace, facts = scenario(h, minimum=2)
    proofs = [checked(h, workspace, [fact]) for fact in facts]
    value = {"items": [activation(fact, proof) for fact, proof in zip(facts, proofs)], "reason": REASON}
    check = preview(h, "approver", value)
    assert check["counts"]["problem"] == 2
    assert {row["issues"][0]["code"] for row in check["items"]} == {"RULE_CHECK_REQUIRED"}
    assert check["groups"][0]["indexes"] == [0, 1]
    assert_waiting(h, facts)


@pytest.mark.parametrize("subset", [{"only": [0]}, {"resolutions": {"1": "skip"}}])
def test_bulk_proof_must_match_the_exact_only_or_skip_subset(h, subset):
    workspace, facts = scenario(h, minimum=1)
    proof = checked(h, workspace, facts)
    value = {"items": [activation(fact, proof) for fact in facts], "reason": REASON, **subset}
    result = h.command("approver", "bulk.apply", value, status=409)
    assert result["error"]["code"] == "RULE_CHECK_REQUIRED"
    assert result["error"]["details"]["completed"] == 0
    assert_waiting(h, facts)


def test_bulk_only_accepts_a_fresh_exact_subset_proof(h):
    workspace, facts = scenario(h, minimum=1)
    proof = checked(h, workspace, facts[:1])
    value = {"items": [activation(fact, proof) for fact in facts], "reason": REASON, "only": [0]}
    result = h.command("approver", "bulk.apply", value)
    assert result["staged_count"] == 1
    with transaction(h.app.state.engine) as db:
        assert db.get(m.SchedulingFact, facts[0]["id"]).status == "active"
        assert db.get(m.SchedulingFact, facts[1]["id"]).status == "proposed"


def test_bulk_fact_union_requires_full_feasibility_even_when_acknowledged(h):
    from copy import deepcopy
    workspace, facts = scenario(h, minimum=1)
    proof = checked(h, workspace, facts)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, proof["id"])
        result = deepcopy(row.result)
        result["full_feasibility_established"] = False
        row.result = result
    value = {"items": [activation(fact, proof) for fact in facts], "reason": REASON}
    for item in value["items"]:
        item["payload"]["acknowledge_unresolved"] = True
    result = h.command("approver", "bulk.apply", value, status=409)
    assert result["error"]["code"] == "RULE_CHECK_UNRESOLVED"
    check = preview(h, "approver", value)
    assert all([option["id"] for option in row["issues"][0]["resolutions"]] == ["skip"] for row in check["items"])
    assert_waiting(h, facts)


def test_bulk_resolution_retry_does_not_consume_failed_attempt_operations(h):
    row, payload, role = relationship(h, "eligibility")
    h.command(role, "eligibility.create", {**payload, "starts_on": "2026-12-01", "ends_on": "2027-03-01"})
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).bulk_settings = {"bulk_max_items": 2, "bulk_direct_max": 2}
    value = {"item_kind": "eligibility.extend", "common": {"ends_on": "2027-02-01"},
             "items": [{"target_id": row["id"], "expected_revision": row["revision"]}],
             "resolutions": {"0": "shift_later_start"}, "reason": REASON}
    check = preview(h, role, value)
    assert check["items"][0]["result"] == "ready", check
    result = h.command(role, "bulk.apply", value)
    assert result["count"] == 1
    assert len(result["authorization_commands"]) == 2


def test_bulk_flush_conflict_is_a_row_issue_and_atomic_failed_index(h, monkeypatch):
    original_dispatch = commands.dispatch
    def conflicting(db, actor, command, settings, *args, **kwargs):
        if command.kind == "catalogue.update" and command.payload.get("value", {}).get("title") == "Synthetic conflict":
            raise IntegrityError("Synthetic constraint", {}, ValueError("Synthetic duplicate"))
        return original_dispatch(db, actor, command, settings, *args, **kwargs)
    monkeypatch.setattr(commands, "dispatch", conflicting)
    value = people_change(h)
    value["items"][1]["changes"][0]["value"] = "Synthetic conflict"
    before = h.get("coordinator", "/api/catalogues")["people"]
    check = preview(h, "coordinator", value)
    assert check["items"][1]["result"] == "problem"
    issue = check["items"][1]["issues"][0]
    assert (issue["code"], issue["message"]) == ("INTEGRITY_CONFLICT", INTEGRITY_CONFLICT_MESSAGE)
    result = h.command("coordinator", "bulk.apply", value, status=409)
    assert result["error"]["code"] == "INTEGRITY_CONFLICT"
    assert result["error"]["details"]["failed_index"] == 1
    assert h.get("coordinator", "/api/catalogues")["people"] == before


@pytest.mark.parametrize("malformed", [{"action": "tag"}, {"action": "tag", "value": {"unexpected": []}}])
def test_bulk_incomplete_tag_change_cannot_clear_existing_members(h, malformed):
    source = h.get("admin", "/api/catalogues")["services"][0]
    tag = h.configure("tags", {"name": "Synthetic retained tag", "labels": ["services"]})["record"]
    h.command("admin", "tag.members", {"add": [{"kind": "services", "id": source["id"]}], "reason": REASON},
              tag["id"], tag["revision"])
    source = next(row for row in h.get("admin", "/api/catalogues")["services"] if row["id"] == source["id"])
    value = {"item_kind": "catalogue.update", "common": {"catalogue": "services"}, "reason": REASON,
             "items": [{"target_id": source["id"], "expected_revision": source["revision"], "changes": [malformed]}]}
    check = preview(h, "admin", value)
    assert check["items"][0]["issues"][0]["code"] == "BULK_TAGS"
    h.command("admin", "bulk.apply", value, status=409)
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.TagMember.id).where(m.TagMember.tag_id == tag["id"], m.TagMember.member_id == source["id"]))
    value["items"][0]["changes"] = [{"action": "tag", "how": "set", "value": []}]
    h.command("admin", "bulk.apply", value)
    with transaction(h.app.state.engine) as db:
        assert not db.scalar(select(m.TagMember.id).where(m.TagMember.tag_id == tag["id"], m.TagMember.member_id == source["id"]))


def test_bulk_rule_line_selection_limit_precedes_policy_compaction(h):
    lines = [{"id": str(uuid.uuid4()), "starts_on": start, "ends_on": end, "numbers": {"max": {"value": 5}}}
             for start, end in [("2026-10-01", "2026-11-01"), ("2026-12-01", "2027-01-01"), ("2027-02-01", "2027-03-01")]]
    proposal = h.command("coordinator", "rule.propose", {"catalogue_id": "duties_per_week", "lines": lines, "reason": REASON})["record"]
    h.command("approver", "policy.activate", {"reason": REASON}, proposal["id"], proposal["revision"])
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).bulk_settings = {"bulk_max_items": 2, "bulk_direct_max": 2}
    value = {"item_kind": "rule.propose", "selection": {"collection": "policy_lines", "where": {"filters": {"status": ["active"]}}},
             "changes": [{"field": "numbers", "value": {"max": {"value": 7}}}], "reason": REASON}
    response = h.client("coordinator").post("/api/commands/preview", json={"kind": "bulk.apply", "payload": value})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "BULK_ITEM_LIMIT"


@pytest.mark.parametrize("preexisting", [False, True])
def test_bulk_failed_item_restores_prior_self_approval_metadata(h, monkeypatch, preexisting):
    from apps.api.auth import authenticate
    from apps.api.services import bulk
    value = people_change(h)
    client = h.client("coordinator")
    def failed(db, actor, command, settings):
        db.info.setdefault("self_approved", []).append({"action": "Synthetic failed approval"})
        raise Problem(409, "SYNTHETIC_REFUSAL", "Synthetic checked refusal")
    monkeypatch.setattr(commands, "dispatch", failed)
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies["rota_session"], client.cookies["rota_csrf"], write=True)
        if preexisting:
            db.info["self_approved"] = [{"action": "Existing outer approval"}]
        with bulk._batch(db, actor, None):
            with pytest.raises(Problem, match="Synthetic checked refusal"):
                bulk._run_item(db, actor, value["items"][0], value, h.settings, None)
            if preexisting:
                assert db.info["self_approved"] == [{"action": "Existing outer approval"}]
            else:
                assert "self_approved" not in db.info
