"""Independent negative material-mutation tests against actual migrated SQLite."""
from .clock_support import freeze_clock
from datetime import datetime, timezone
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from apps.api import models as m, auth
from apps.api.db import transaction
from .governance_support import identity, route, transition
from .test_review_routes import delegation
from .test_staff_swaps import accepted, offered, prepared, source_pair


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    now = datetime(2026, 9, 18, 10, tzinfo=timezone.utc)
    freeze_clock(monkeypatch, lambda: now)
    monkeypatch.setattr(auth, "now", lambda: now)


def rejects_sql(h, statement, target):
    with pytest.raises(DBAPIError), transaction(h.app.state.engine, write=True) as db:
        db.execute(text(statement), {"id": target})


@pytest.mark.parametrize("statement", [
    "UPDATE review_routes SET definition='{}' WHERE id=:id",
    "UPDATE review_routes SET starts_on='2026-09-01' WHERE id=:id",
    "UPDATE review_routes SET name='Rewritten route' WHERE id=:id",
    "UPDATE review_routes SET activated_by=created_by WHERE id=:id",
    "UPDATE review_routes SET status='proposed' WHERE id=:id",
    "DELETE FROM review_routes WHERE id=:id",
])
def test_activated_route_material_cannot_be_rewritten(h, statement):
    r = route(h)
    rejects_sql(h, statement, r["id"])
    assert h.get("admin", "/api/review-routes")["routes"][0]["status"] == "active"


def test_retired_route_cannot_be_resurrected_or_deleted(h):
    r = route(h)
    h.command("approver", "review_route.retire", {"reason": "Explicitly retire route"}, r["id"], r["revision"])
    rejects_sql(h, "UPDATE review_routes SET status='active' WHERE id=:id", r["id"])
    rejects_sql(h, "DELETE FROM review_routes WHERE id=:id", r["id"])


def test_every_proposal_author_is_excluded_from_route_activation(h):
    identity(h, "author_one", ["admin", "approver"])
    payload = {"name": "Two-author proposal", "starts_on": "2026-10-01", "definition": {
        "steps": [{"key": "clinical", "label": "Independent review"}]}, "reason": "Original configuration"}
    r = h.command("author_one", "review_route.create", payload)["route"]
    r = h.command("admin", "review_route.update", {**payload, "name": "Second author's amendment"}, r["id"], r["revision"])["route"]
    assert h.command("author_one", "review_route.activate", {"reason": "Cannot approve own proposal"},
                     r["id"], r["revision"], status=403)["error"]["code"] == "SELF_APPROVAL"
    assert h.command("approver", "review_route.activate", {"reason": "Independent from both authors"},
                     r["id"], r["revision"])["route"]["status"] == "active"


@pytest.mark.parametrize("statement", [
    "UPDATE review_delegations SET expires_at='2030-01-01' WHERE id=:id",
    "UPDATE review_delegations SET starts_at='2020-01-01' WHERE id=:id",
    "UPDATE review_delegations SET step_key='weaker_step' WHERE id=:id",
    "UPDATE review_delegations SET route_revision=99 WHERE id=:id",
    "UPDATE review_delegations SET reason='changed authority' WHERE id=:id",
    "DELETE FROM review_delegations WHERE id=:id",
])
def test_delegation_material_is_immutable_but_revocation_is_supported(h, statement):
    identity(h, "programme", ["programme_lead"])
    r = route(h, [{"key": "programme", "label": "Programme review", "role": "programme_lead"}])
    d = delegation(h, r)
    rejects_sql(h, statement, d["id"])
    d = h.command("programme", "review_delegation.revoke", {"reason": "Revocation remains possible"}, d["id"], d["revision"])["delegation"]
    assert d["revoked"]
    rejects_sql(h, "UPDATE review_delegations SET revoked=false WHERE id=:id", d["id"])


@pytest.mark.parametrize("statement", [
    "UPDATE swap_requests SET expires_at='2030-01-01' WHERE id=:id",
    "UPDATE swap_requests SET recipient_person_id=proposer_person_id WHERE id=:id",
    "UPDATE swap_requests SET first_assignment_id=second_assignment_id WHERE id=:id",
    "UPDATE swap_requests SET second_assignment_id=first_assignment_id WHERE id=:id",
    "UPDATE swap_requests SET recipient_id=proposer_id WHERE id=:id",
    "UPDATE swap_requests SET reason='rewritten consent reason' WHERE id=:id",
    "UPDATE swap_requests SET status='offered' WHERE id=:id",
    "DELETE FROM swap_requests WHERE id=:id",
])
def test_accepted_pair_and_expiry_cannot_be_rewritten(h, statement):
    row, _ = accepted(h)
    rejects_sql(h, statement, row["id"])
    assert h.get("staff", "/api/swaps/" + row["id"])["swap"]["status"] == "accepted"
    assert transition(h, row, "withdraw")["swap"]["status"] == "withdrawn"
    rejects_sql(h, "UPDATE swap_requests SET status='accepted' WHERE id=:id", row["id"])


def test_direct_state_skip_and_prepared_hash_rewrite_are_rejected(h):
    pid, first, _, _ = source_pair(h)
    row = offered(h, pid, first)
    rejects_sql(h, "UPDATE swap_requests SET status='accepted' WHERE id=:id", row["id"])


def test_prepared_and_published_request_material_remains_frozen(h):
    row, w, _ = prepared(h)
    rejects_sql(h, "UPDATE swap_requests SET expected_content_hash='forged' WHERE id=:id", row["id"])
    rejects_sql(h, "UPDATE swap_requests SET workspace_id=NULL WHERE id=:id", row["id"])
    h.publish(w)
    for sql in ("UPDATE swap_requests SET status='withdrawn' WHERE id=:id",
                "UPDATE swap_requests SET result_publication_id=NULL WHERE id=:id",
                "DELETE FROM swap_requests WHERE id=:id"):
        rejects_sql(h, sql, row["id"])


def test_inactive_person_cannot_read_a_swap_by_known_id(h):
    row, _ = accepted(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, h.seed["people"][0]).active = False
    assert h.get("staff", "/api/swaps/" + row["id"], status=403)["error"]["code"] == "STAFF_BINDING_REQUIRED"
    assert h.get("coordinator", "/api/swaps/" + row["id"])["swap"]["effective_status"] == "access_revoked"


def test_recovery_preserves_new_consent_review_views_and_guards_but_revokes_sessions(h, tmp_path):
    from .conftest import Harness
    from scripts.backup import backup, restore
    row, w, _ = prepared(h)
    route(h)
    h.command('coordinator','planner_view.create',{'name':'Recovered private preference','definition':{'mode':'month'}})
    published = h.publish(w)
    models = [m.SwapRequest,m.SwapEvent,m.ReviewRoute,m.ReviewDecision,m.PlannerView,m.Publication,m.AuditEvent]
    from sqlalchemy import select, func
    with transaction(h.app.state.engine) as db:
        before = [db.scalar(select(func.count()).select_from(cls)) for cls in models]
    archive = tmp_path/'governance-backup.sqlite'
    backup(h.settings.database_url,archive)
    restored_url='sqlite:///'+str(tmp_path/'recovered.sqlite')
    restored=restore(archive,restored_url,trusted_source=True)
    assert restored['sessions_revoked']
    recovered=Harness(restored_url,h.seed)
    try:
        with transaction(recovered.app.state.engine) as db:
            assert [db.scalar(select(func.count()).select_from(cls)) for cls in models]==before
            assert all(s.revoked for s in db.scalars(select(m.Session)))
        assert recovered.get('staff','/api/swaps/'+row['id'])['swap']['result_publication_id']==published['publication_id']
        assert recovered.get('coordinator','/api/planner/views')['views'][0]['definition']['mode']=='month'
        rejects_sql(recovered,"UPDATE swap_events SET reason='tampered restore'",row['id'])
        rejects_sql(recovered,"UPDATE swap_requests SET status='offered' WHERE id=:id",row['id'])
    finally:
        for client in recovered.clients.values():client.close()
        recovered.app.state.engine.dispose()
