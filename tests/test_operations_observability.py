"""Constructed queue observations, not measured production throughput or SLOs."""
from .clock_support import freeze_clock
import json
import uuid
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import func, select
from apps.api import models as m
from apps.api.db import transaction
from packages.domain.worker_contract import JOB_LEASE_SECONDS

NOW = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)


def outbox(db, dept, state="queued", *, created=NOW, due=NOW, **values):
    event = m.Outbox(department_id=dept, event_key=str(uuid.uuid4()), kind="request", payload={"private_reason": "DO NOT DISCLOSE"},
                     state=state, created_at=created, next_attempt_at=due, **values)
    db.add(event); db.flush()
    return event.id


@pytest.mark.parametrize("role", ["admin", "coordinator", "approver", "credentialer", "staff"])
def test_observability_requires_explicit_operator_or_auditor(h, role):
    h.get(role, "/api/operations?state=failed", status=403)


@pytest.mark.parametrize("query", ["offset=-1", "offset=1000001", "offset=abc", "state=delivered", "state="])
def test_observability_rejects_invalid_page_and_state(h, query):
    h.get("operator", "/api/operations?" + query, status=422)


def test_all_old_failed_outbox_events_remain_discoverable_without_payloads(h):
    with transaction(h.app.state.engine, write=True) as db:
        dept = h.seed["department_id"]
        older = outbox(db, dept, "failed", created=NOW - timedelta(days=1))
        for _ in range(103): outbox(db, dept, "available_in_app")
    first = h.get("operator", "/api/operations")
    assert first["total"] == 104 and first["has_more"] and len(first["outbox"]) == 100
    second = h.get("operator", "/api/operations?offset=100")
    assert not second["has_more"] and len(second["outbox"]) == 4
    assert not ({r["id"] for r in first["outbox"]} & {r["id"] for r in second["outbox"]})
    only_failed = h.get("auditor", "/api/operations?state=failed")
    assert only_failed["total"] == 1 and only_failed["outbox"][0]["id"] == older
    assert "DO NOT DISCLOSE" not in json.dumps(first) + json.dumps(second) + json.dumps(only_failed)
    assert all("payload" not in r for r in first["outbox"])
    h.command("auditor", "outbox.retry", {"reason": "Read authority cannot retry"}, older, 1, status=403)


def test_queue_measurements_are_scoped_read_only_and_explicit_about_unknowns(h, monkeypatch):
    w = h.workspace()
    h.client("operator")  # Login before measuring append-only operation counts.
    with transaction(h.app.state.engine, write=True) as db:
        dept = h.seed["department_id"]
        other = m.Department(organisation_id=db.get(m.Department, dept).organisation_id, name="Foreign department")
        db.add(other); db.flush()
        outbox(db, other.id, "failed")
        outbox(db, dept, due=NOW-timedelta(seconds=30))
        outbox(db, dept, "retry", due=NOW+timedelta(seconds=20))
        for state, start, created in [("queued", None, NOW-timedelta(seconds=90)),
                                      ("running", NOW-timedelta(seconds=JOB_LEASE_SECONDS+1), NOW),
                                      ("running", None, NOW), ("succeeded", NOW, NOW)]:
            db.add(m.Job(department_id=dept, kind="validation", workspace_id=w["id"], requested_by=h.seed["identities"]["coordinator"],
                         status=state, started_at=start, created_at=created, input_fingerprint="f"*64, input_snapshot={"secret": "DO NOT DISCLOSE"}))
        db.add(m.ImportBatch(department_id=dept, workspace_id=w["id"], source_name="PRIVATE FILENAME", source_hash="e"*64,
                             rows=[{"sensitive": "DO NOT DISCLOSE"}], created_by=h.seed["identities"]["coordinator"],
                             workspace_revision=w["revision"], created_at=NOW-timedelta(seconds=120)))
    freeze_clock(monkeypatch, lambda: NOW)
    with transaction(h.app.state.engine) as db:
        before = {cls.__tablename__: db.scalar(select(func.count()).select_from(cls)) for cls in (m.AuditEvent, m.CommandReceipt, m.Outbox, m.Job)}
    response = h.get("operator", "/api/operations")
    metrics = response["metrics"]
    assert metrics["outbox_states"] == {"queued": 1, "retry": 1, "failed": 0, "available_in_app": 0, "superseded": 0, "sent": 0}
    assert metrics["outbox_due_count"] == 1 and metrics["outbox_oldest_due_age_seconds"] == 30
    assert metrics["oldest_queued_job_age_seconds"] == 90
    assert metrics["expired_running_job_leases"] == 1 and metrics["running_jobs_without_lease_start"] == 1
    assert metrics["latest_import_received_age_seconds"] == 120
    assert metrics["worker_liveness"] == "never_seen" and metrics["source_completeness"] == "unqualified"
    assert "PRIVATE FILENAME" not in json.dumps(response) and "DO NOT DISCLOSE" not in json.dumps(response)
    with transaction(h.app.state.engine) as db:
        after = {cls.__tablename__: db.scalar(select(func.count()).select_from(cls)) for cls in (m.AuditEvent, m.CommandReceipt, m.Outbox, m.Job)}
    assert before == after


def test_empty_and_future_dated_measurements_do_not_imply_health(h, monkeypatch):
    h.client("operator")
    freeze_clock(monkeypatch, lambda: NOW)
    empty = h.get("operator", "/api/operations")["metrics"]
    assert empty["outbox_oldest_due_age_seconds"] is None and empty["latest_import_received_at"] is None
    assert empty["worker_liveness"] == "never_seen"  # no worker has checked in, so not "healthy"
    with transaction(h.app.state.engine, write=True) as db:
        outbox(db, h.seed["department_id"], created=NOW+timedelta(days=1), due=NOW+timedelta(days=1))
    assert h.get("operator", "/api/operations")["metrics"]["outbox_due_count"] == 0


def test_open_operator_session_loses_observability_after_revocation(h):
    h.get("operator", "/api/operations")
    with transaction(h.app.state.engine, write=True) as db:
        db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == h.seed["identities"]["operator"])).revoked = True
    h.get("operator", "/api/operations", status=403)
