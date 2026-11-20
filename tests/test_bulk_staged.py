from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from apps.api import models as m
from apps.api.db import retry_write, transaction
from apps.worker import main as worker
from packages.domain.time import aware
from .governance_support import identity


def staged_change(h):
    row = h.command("coordinator", "availability.record", {
        "person_id": h.seed["people"][0], "kind": "actual_absence",
        "start": "2026-10-04T10:00:00+03:00", "end": "2026-10-04T11:00:00+03:00",
        "private_reason": "Synthetic private leave detail"})["record"]
    payload = {"item_kind": "availability.retract", "reason": "Resolve this staged test change",
        "items": [{"target_id": row["id"], "expected_revision": row["revision"], "payload": {}}],
        "resolutions": {"0": "skip"}}
    return row, h.command("coordinator", "bulk.stage", {"payload": payload})["staged"]


def test_reopen_missing_staged_change_returns_not_found(h):
    response = h.client("coordinator").post(f"/api/bulk/staged/{uuid4()}/reopen")
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_staged_changes_are_owner_only_and_reopen_rechecks_without_saving(h):
    row, staged = staged_change(h)
    mine = h.get("coordinator", "/api/bulk/staged")
    assert mine["count"] == 1
    assert mine["items"][0]["id"] == staged["id"]
    assert mine["items"][0]["payload"]["items"][0]["expected_revision"] == row["revision"]
    assert mine["items"][0]["payload"]["resolutions"] == {"0": "skip"}
    assert h.get("credentialer", "/api/bulk/staged") == {"items": [], "count": 0}
    other = h.client("credentialer").post(f"/api/bulk/staged/{staged['id']}/reopen")
    assert other.status_code == 404
    reopened = h.client("coordinator").post(f"/api/bulk/staged/{staged['id']}/reopen")
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["preview"]["preview"] is True
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Availability, row["id"]).retracted is False
        assert db.get(m.Availability, row["id"]).revision == row["revision"]


def test_reopen_keeps_original_target_revision_and_reports_concurrent_edit(h):
    row, staged = staged_change(h)
    h.command("coordinator", "availability.retract", {"reason": "A separate later change"},
              row["id"], row["revision"])
    with transaction(h.app.state.engine, write=True) as db:
        saved = db.get(m.BulkStaged, staged["id"])
        saved.payload = {**saved.payload, "resolutions": {}}
    response = h.client("coordinator").post(f"/api/bulk/staged/{staged['id']}/reopen")
    assert response.status_code == 200, response.text
    result = response.json()["preview"]["items"][0]
    assert result["result"] == "problem"
    assert result["issues"][0]["code"] == "STALE_REVISION"
    assert response.json()["staged"]["payload"]["items"][0]["expected_revision"] == row["revision"]


def test_drop_requires_owner_and_current_revision_and_preserves_history(h):
    _, staged = staged_change(h)
    h.command("credentialer", "bulk.staged.drop", {"reason": "Cannot drop someone else's change"},
              staged["id"], staged["revision"], status=404)
    stale = h.command("coordinator", "bulk.staged.drop", {"reason": "Stale stage revision"},
                      staged["id"], staged["revision"] + 1, status=409)
    assert stale["error"]["code"] == "STALE_REVISION"
    dropped = h.command("coordinator", "bulk.staged.drop", {"reason": "Discard these unapplied changes"},
                        staged["id"], staged["revision"])["staged"]
    assert dropped["status"] == "dropped"
    assert dropped["revision"] == staged["revision"] + 1
    assert h.get("coordinator", "/api/bulk/staged")["count"] == 0
    with transaction(h.app.state.engine) as db:
        saved = db.get(m.BulkStaged, staged["id"])
        assert saved.payload == {}
        history = list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == "bulk_staged", m.DefinitionHistory.object_id == staged["id"])
            .order_by(m.DefinitionHistory.object_revision)))
        assert [note.snapshot["status"] for note in history] == ["active", "dropped"]
        assert all("payload" not in note.snapshot for note in history)


def test_staged_expiry_uses_department_setting_and_writes_history_once(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).bulk_settings = {"bulk_staged_days": 3}
    _, staged = staged_change(h)
    with transaction(h.app.state.engine, write=True) as db:
        saved = db.get(m.BulkStaged, staged["id"])
        assert aware(saved.expires_at) == aware(saved.created_at) + timedelta(days=3)
        saved.expires_at = m.now() - timedelta(seconds=1)
    expired = h.client("coordinator").post(f"/api/bulk/staged/{staged['id']}/reopen")
    assert expired.status_code == 200, expired.text
    assert expired.json()["expired"] is True and expired.json()["preview"] is None
    assert h.get("coordinator", "/api/bulk/staged")["count"] == 0
    assert h.get("coordinator", "/api/bulk/staged")["count"] == 0
    with transaction(h.app.state.engine) as db:
        saved = db.get(m.BulkStaged, staged["id"])
        assert saved.status == "expired"
        assert saved.payload == {}
        audit = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "bulk.staged.expire",
                                                        m.AuditEvent.target_id == staged["id"])))
        assert len(audit) == 1
        notes = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == "bulk_staged",
            m.DefinitionHistory.object_id == staged["id"], m.DefinitionHistory.object_revision == saved.revision)))
        assert len(notes) == 1 and "expired" in notes[0].reason


def test_applying_reopened_staging_closes_original_and_keeps_target_revision(h):
    row, staged = staged_change(h)
    payload = {**staged["payload"], "resolutions": {}, "staged_id": staged["id"]}
    command = {"kind": "bulk.apply", "payload": payload}
    client = h.client("coordinator")
    checked = client.post("/api/commands/preview", json=command)
    assert checked.status_code == 200, checked.text
    response = client.post("/api/commands", headers={"idempotency-key": str(uuid4())},
        json={**command, "expected_fingerprint": checked.json()["source_fingerprint"]})
    assert response.status_code == 200, response.text
    assert h.get("coordinator", "/api/bulk/staged")["count"] == 0
    with transaction(h.app.state.engine) as db:
        closed = db.get(m.BulkStaged, staged["id"])
        assert closed.status == "applied" and closed.payload == {}
        assert db.get(m.Availability, row["id"]).retracted is True


def test_independence_handoff_notifies_only_other_qualified_people_and_rechecks_on_apply(h):
    person = h.seed["people"][0]
    identity(h, "own_credentialer", ["credentialer"], person=person)
    identity(h, "same_person_login", ["credentialer"], person=person)
    service = h.configure("services", {"name": "Synthetic independently reviewed service", "starts_on": "2020-01-01"})["record"]
    payload = {"item_kind": "eligibility.create", "reason": "Request independent clinical permission review",
        "items": [{"payload": {"person_id": person, "service_id": service["id"], "starts_on": "2026-10-01"}}]}
    requester = h.client("own_credentialer")
    check = requester.post("/api/commands/preview", json={"kind": "bulk.apply", "payload": payload})
    assert check.status_code == 200, check.text
    issue = check.json()["items"][0]["issues"][0]
    assert issue["code"] == "SELF_APPROVAL" and issue["handoff"]["role"] == "credentialer"
    assert "send_for_approval" in {resolution["id"] for resolution in issue["resolutions"]}
    sent = h.command("own_credentialer", "bulk.apply", {**payload, "resolutions": {"0": "send_for_approval"}})
    review = sent["reviews"][0]
    assert sent["counts"]["waiting"] == 1
    assert review["status"] == "waiting" and review["review_role"] == "credentialer"
    assert review["payload"]["resolutions"] == {}
    assert h.get("own_credentialer", "/api/bulk/staged")["items"][0]["id"] == review["id"]
    assert h.get("same_person_login", "/api/bulk/staged")["count"] == 0
    assert h.get("approver", "/api/bulk/staged")["count"] == 0
    assert h.get("credentialer", "/api/bulk/staged")["items"][0]["id"] == review["id"]
    h.command("credentialer", "bulk.staged.drop", {"reason": "Reviewers cannot discard the requester's item"},
              review["id"], review["revision"], status=404)
    apply_payload = {**review["payload"], "staged_id": review["id"]}
    refused = h.command("own_credentialer", "bulk.apply", apply_payload, status=403)
    assert refused["error"]["code"] == "SELF_APPROVAL"
    changed = h.command("credentialer", "bulk.apply", {**apply_payload,
        "reason": "Substitute a different change under an existing review"}, status=409)
    assert changed["error"]["code"] == "BULK_STAGED_CHANGED"
    with transaction(h.app.state.engine) as db:
        assert not db.scalar(select(m.Eligibility.id).where(m.Eligibility.service_id == service["id"]))
    retry_write(h.app.state.engine, lambda db: worker.dispatch_batch(db))
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.Outbox).where(m.Outbox.kind == "bulk_review"))
        notifications = list(db.scalars(select(m.Notification).where(m.Notification.outbox_id == event.id)))
        assert {note.identity_id for note in notifications} == {
            h.seed["identities"]["credentialer"], h.seed["identities"]["admin"]}
        assert all("independent reviewer" in note.title and "Bulk editor" in note.message for note in notifications)
    inbox = h.get("credentialer", "/api/notifications")["notifications"]
    assert next(note for note in inbox if note["kind"] == "bulk_review")["link"] == {"staged_id": review["id"]}
    reviewer = h.client("credentialer")
    reopened = reviewer.post(f"/api/bulk/staged/{review['id']}/reopen")
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["preview"]["counts"]["ready"] == 1
    accepted = reviewer.post("/api/commands", headers={"idempotency-key": str(uuid4())}, json={
        "kind": "bulk.apply", "payload": apply_payload,
        "expected_fingerprint": reopened.json()["preview"]["source_fingerprint"]})
    assert accepted.status_code == 200, accepted.text
    with transaction(h.app.state.engine) as db:
        assert db.get(m.BulkStaged, review["id"]).status == "applied"
        assert db.scalar(select(m.Eligibility.id).where(m.Eligibility.service_id == service["id"]))
        note = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == "bulk_staged",
            m.DefinitionHistory.object_id == review["id"], m.DefinitionHistory.object_revision == 2))
        assert note.actor_id == h.seed["identities"]["credentialer"]
        assert "payload" not in note.snapshot
