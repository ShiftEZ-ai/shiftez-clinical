"""Review and issuance contracts on the migrated PostgreSQL schema."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from apps.api import models as m
from apps.api.db import transaction

from .governance_support import identity, route, transition
from .test_review_routes import delegation
from .test_staff_swaps import accepted, ordered_clock

pytestmark = pytest.mark.postgres


def refuse_write(pg, statement, target, message):
    with pytest.raises(DBAPIError, match=message), transaction(pg.app.state.engine, write=True) as db:
        db.execute(text(statement), {"target": target})


def test_migrated_publication_scopes_duties_and_workload_to_staff(pg):
    own, other = pg.seed["people"][:2]
    workspace = pg.shift(pg.workspace())["workspace"]
    workspace = pg.shift(workspace, start="2026-10-05T08:00:00+03:00",
                         end="2026-10-05T10:00:00+03:00", people=[other])["workspace"]
    result = pg.publish(workspace)
    assert result["sequence"] == 1
    publication = result["publication_id"]

    team = pg.get("coordinator", f"/api/publications/{publication}")["snapshot"]["shifts"]
    personal = pg.get("staff", f"/api/publications/{publication}")
    duty, = personal["snapshot"]["shifts"]
    assert len(team) == 2 and duty["id"] == team[0]["id"]
    assert [entry["person_id"] for entry in duty["participants"]] == [own]
    assert personal["original_validation"] == {"valid_at_publication": True}

    path = f"/api/reports/{publication}"
    team_rows = pg.get("coordinator", path)["rows"]
    assert {row["person_id"]: row["scheduled_hours"] for row in team_rows} == {own: 4, other: 2}
    row, = pg.get("staff", path)["rows"]
    assert (row["person_id"], row["assignments"], row["scheduled_hours"]) == (own, 1, 4)
    assert pg.get("staff", f"{path}?person_id={other}", status=403)["error"]["code"] == "FORBIDDEN"


def test_issued_assignment_snapshot_and_audit_history_are_immutable(pg):
    workspace = pg.shift(pg.workspace())["workspace"]
    issued = pg.publish(workspace)["publication_id"]
    with transaction(pg.app.state.engine) as db:
        publication = db.get(m.Publication, issued)
        snapshot, validation = publication.snapshot, publication.validation
        sid = snapshot["shifts"][0]["id"]
        assignment_id = db.scalars(select(m.Assignment.id).where(m.Assignment.shift_id == sid)).one()
        audit_id = db.scalars(select(m.AuditEvent.id).where(
            m.AuditEvent.department_id == pg.seed["department_id"]).limit(1)).one()

    refuse_write(pg, "UPDATE assignments SET overtime=NOT overtime WHERE id=:target",
                 assignment_id, "published duties are immutable")
    refuse_write(pg, "UPDATE publications SET snapshot='{}' WHERE id=:target",
                 issued, "immutable publication")
    refuse_write(pg, "DELETE FROM audit_events WHERE id=:target", audit_id, "immutable evidence")

    with transaction(pg.app.state.engine) as db:
        publication = db.get(m.Publication, issued)
        assert (publication.snapshot, publication.validation, publication.current) == (snapshot, validation, True)
        assert db.get(m.Assignment, assignment_id).overtime is False
        assert db.get(m.AuditEvent, audit_id) is not None


def test_sequential_review_requires_current_independent_reviewers(pg):
    identity(pg, "programme", ["programme_lead"])
    identity(pg, "middle_editor", ["coordinator", "programme_lead"])
    route(pg, [{"key": "programme", "label": "Programme review", "role": "programme_lead"},
               {"key": "clinical", "label": "Clinical review", "role": "approver"}])
    workspace = pg.shift(pg.workspace(), role="middle_editor")["workspace"]
    workspace = pg.lifecycle(workspace, "submit")["workspace"]

    for actor, code in (("approver", "REVIEW_STEP_ROLE"), ("middle_editor", "SELF_APPROVAL")):
        assert pg.lifecycle(workspace, "approve", actor, status=403)["error"]["code"] == code
    workspace = pg.lifecycle(workspace, "approve", "programme")["workspace"]
    progress = pg.detail(workspace)["review_progress"]
    assert progress["pending_step"]["key"] == "clinical" and workspace["status"] == "submitted"
    assert pg.lifecycle(workspace, "publish", status=409)["error"]["code"] == "WORKFLOW_STATE"
    workspace = pg.lifecycle(workspace, "approve", "approver")["workspace"]
    assert workspace["status"] == "approved" and pg.detail(workspace)["review_progress"]["complete"]

    with transaction(pg.app.state.engine, write=True) as db:
        db.get(m.Identity, pg.seed["identities"]["programme"]).disabled = True
    assert pg.lifecycle(workspace, "publish", status=409)["error"]["code"] == "APPROVER_REVOKED"
    assert pg.get("coordinator", "/api/publications")["publications"] == []

    with transaction(pg.app.state.engine, write=True) as db:
        db.get(m.Identity, pg.seed["identities"]["programme"]).disabled = False
    issued = pg.lifecycle(workspace, "publish")["publication_id"]
    path = f"/api/publications/{issued}"
    assert pg.get("coordinator", path)["snapshot"]["workspace"]["review_snapshot"]["steps"]
    assert "review_snapshot" not in pg.get("staff", path)["snapshot"]["workspace"]


def test_consented_exchange_issues_only_agreed_duties_and_keeps_evidence(pg, monkeypatch):
    with ordered_clock(monkeypatch):
        consent, before = accepted(pg, third=True)
        prepared = transition(pg, consent, "prepare", "coordinator")
        workspace = prepared["workspace"]
        submitted = pg.lifecycle(workspace, "submit")["workspace"]
        assert pg.lifecycle(submitted, "publish", status=409)["error"]["code"] == "WORKFLOW_STATE"
        approved = pg.lifecycle(submitted, "approve", "approver")["workspace"]
        issued = pg.lifecycle(approved, "publish")["publication_id"]

        path = f"/api/swaps/{consent['id']}"
        evidence = pg.get("staff", path)
        assert evidence["swap"]["result_publication_id"] == issued
        assert [event["action"] for event in evidence["events"]] == [
            "offered", "counteroffer", "confirm", "prepare", "published",
        ]
        after = pg.get("coordinator", f"/api/publications/{issued}")["snapshot"]
        first, second, untouched = pg.seed["people"][:3]
        assert [(duty["start"], duty["end"], duty["service_ids"], duty["location_id"])
                for duty in after["shifts"]] == [
                    (duty["start"], duty["end"], duty["service_ids"], duty["location_id"])
                    for duty in before["shifts"]
                ]
        assert [[p["person_id"] for p in duty["participants"]] for duty in after["shifts"]] == [
            [second], [first], [untouched],
        ]
        source = pg.get("coordinator", f"/api/publications/{consent['publication_id']}")
        assert source["snapshot"] == before and source["publication"]["current"] is False

        with transaction(pg.app.state.engine) as db:
            review_id = db.scalars(select(m.ReviewDecision.id).where(
                m.ReviewDecision.workspace_id == workspace["id"])).one()
            event_id = db.scalars(select(m.SwapEvent.id).where(
                m.SwapEvent.swap_id == consent["id"]).limit(1)).one()
        refuse_write(pg, "UPDATE swap_requests SET expected_content_hash='rewritten' WHERE id=:target",
                     consent["id"], "immutable authority or consent material")
        refuse_write(pg, "UPDATE swap_events SET reason='rewritten' WHERE id=:target",
                     event_id, "immutable evidence")
        refuse_write(pg, "DELETE FROM review_decisions WHERE id=:target", review_id, "immutable evidence")
        assert pg.get("staff", path) == evidence


def test_activated_route_and_delegation_material_are_immutable(pg):
    identity(pg, "programme", ["programme_lead"])
    activated = route(pg, [{"key": "programme", "label": "Programme review", "role": "programme_lead"}])
    delegated = delegation(pg, activated)

    refuse_write(pg, "UPDATE review_routes SET definition='{}' WHERE id=:target",
                 activated["id"], "immutable authority or consent material")
    refuse_write(pg, "UPDATE review_delegations SET expires_at=expires_at+interval '1 day' WHERE id=:target",
                 delegated["id"], "immutable authority or consent material")
    refuse_write(pg, "DELETE FROM review_delegations WHERE id=:target",
                 delegated["id"], "retained authority or consent evidence")
    assert pg.get("admin", "/api/review-routes")["routes"] == [activated]
    revoked = pg.command("programme", "review_delegation.revoke", {"reason": "End delegated review"},
                         delegated["id"], delegated["revision"])["delegation"]
    assert revoked["revoked"] is True
    refuse_write(pg, "UPDATE review_delegations SET revoked=false WHERE id=:target",
                 delegated["id"], "immutable authority or consent material")
