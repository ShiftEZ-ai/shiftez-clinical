"""Feedback migration, private storage, bounded image retention, and backup preservation."""
from contextlib import closing
from datetime import timedelta
from pathlib import Path
import sqlite3

from alembic import command
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import MetaData, Table, func, inspect, select
from sqlalchemy.exc import IntegrityError

from apps.api import models as m
from apps.api.db import make_engine, transaction
from apps.api.serialization import record
from apps.worker import main as worker
from migrations import safety
from packages.contracts.schemas import FEEDBACK_LIMITS
from scripts import backup, manage, restore_drill
from .migration_support import historical_seed


IMAGE = b"\x89PNG\r\n\x1a\nprivate synthetic screenshot"


def report(h, **changes):
    return m.FeedbackReport(**{
        "department_id": h.seed["department_id"],
        "reporter_identity_id": h.seed["identities"]["staff"],
        "reporter_person_id": h.seed["people"][0],
        "kind": "problem", "message": "Synthetic feedback", "route": "#/home?range=week",
        "environment": "dev", "viewport": {"w": 1440, "h": 900}, "fingerprint": "f" * 64,
        **changes,
    })


def snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        tables = sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                        if row[0] != "alembic_version")
        return {name: db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in tables}


def test_feedback_migration_preserves_existing_data_and_requires_review(tmp_path, monkeypatch):
    monkeypatch.setattr('apps.api.services.event_defaults.seed_department', lambda *args: None)
    path = tmp_path / "before-feedback.db"
    url = "sqlite:///" + str(path)
    command.upgrade(manage.alembic_config(url), "0066_ux_audit_b1")
    engine = make_engine(url)
    with transaction(engine, write=True) as db:
        seed = historical_seed(db)
        db.add(m.AuditEvent(department_id=seed["department_id"], actor_id=seed["identities"]["staff"],
                            action="test.evidence", reason="Synthetic existing evidence", detail={"revision": 1},
                            correlation_id="synthetic-storage-test"))
    engine.dispose()
    before = snapshot(path)
    inspected = restore_drill.inspect(url)
    assert inspected["head_ok"] is False and "feedback_reports" not in inspected["counts"]
    command.upgrade(manage.alembic_config(url), "0067_feedback_reports")
    after = snapshot(path)
    assert set(after) == set(before) | {"feedback_reports"}
    # Later additive revisions may append columns; every original row and value must survive.
    for name, rows in before.items():
        assert len(after[name]) == len(rows)
        assert [row[:len(old)] for row, old in zip(after[name], rows)] == rows
    assert after["feedback_reports"] == []
    migration = Path("migrations/versions/0067_feedback_reports.py")
    # SQL length() in CHECK constraints remains outside the source proof allowlist.
    assert safety.classification(migration) == "destructive"
    assert safety.findings(migration) and safety.needs_confirmation(migration)
    assert safety.declaration_errors(migration) == [] and safety.unreviewed(migration) == []
    assert "length()" in safety.marker(migration)


def test_recent_routes_migration_preserves_legacy_reports_and_other_data(tmp_path):
    path = tmp_path / "before-recent-routes.db"
    url = "sqlite:///" + str(path)
    config = manage.alembic_config(url)
    revision = ScriptDirectory.from_config(config).get_revision("0106_feedback_recent_routes")
    command.upgrade(config, revision.down_revision)
    engine = make_engine(url)
    legacy = Table("feedback_reports", MetaData(), autoload_with=engine)
    with transaction(engine, write=True) as db:
        seed = historical_seed(db)
        db.execute(legacy.insert().values(id=m.uid(), revision=1, created_at=m.now(), updated_at=m.now(),
            department_id=seed["department_id"], reporter_identity_id=seed["identities"]["staff"],
            reporter_person_id=seed["people"][0], kind="problem", message="Synthetic existing feedback",
            route="#home?private=existing", environment="dev", viewport={"w": 1440, "h": 900},
            fingerprint="f" * 64, screenshot=IMAGE, screenshot_media_type="image/png"))
        old_report = dict(db.execute(legacy.select()).mappings().one())
    engine.dispose()
    before = snapshot(path)
    command.upgrade(config, revision.revision)
    after = snapshot(path)
    assert set(after) == set(before)
    assert {name: rows for name, rows in after.items() if name != "feedback_reports"} == {
        name: rows for name, rows in before.items() if name != "feedback_reports"}
    migrated = Table("feedback_reports", MetaData(), autoload_with=engine)
    with transaction(engine) as db:
        value = dict(db.execute(migrated.select()).mappings().one())
        assert value.pop("recent_routes") == []
        assert value == old_report
    engine.dispose()
    migration = Path(revision.path)
    assert safety.findings(migration) == [] and safety.unreviewed(migration) == []


def test_github_reservation_migration_preserves_existing_feedback(tmp_path):
    path = tmp_path / "before-github-reservation.db"
    url = "sqlite:///" + str(path)
    config = manage.alembic_config(url)
    revision = ScriptDirectory.from_config(config).get_revision("0110_feedback_github_reservation")
    command.upgrade(config, revision.down_revision)
    engine = make_engine(url)
    legacy = Table("feedback_reports", MetaData(), autoload_with=engine)
    with transaction(engine, write=True) as db:
        seed = historical_seed(db)
        db.execute(legacy.insert().values(id=m.uid(), revision=1, created_at=m.now(), updated_at=m.now(),
            department_id=seed["department_id"], reporter_identity_id=seed["identities"]["staff"],
            kind="problem", message="Synthetic existing feedback", route="#home", environment="dev",
            viewport={"w": 1440, "h": 900}, fingerprint="f" * 64, screenshot=IMAGE, screenshot_media_type="image/png"))
        old_report = dict(db.execute(legacy.select()).mappings().one())
    engine.dispose()
    before = snapshot(path)
    command.upgrade(config, revision.revision)
    after = snapshot(path)
    assert {name: rows for name, rows in after.items() if name != "feedback_reports"} == {
        name: rows for name, rows in before.items() if name != "feedback_reports"}
    with transaction(engine) as db:
        migrated = Table("feedback_reports", MetaData(), autoload_with=engine)
        row = dict(db.execute(migrated.select()).mappings().one())
        assert row.pop("github_export_repo") is None and row == old_report
    engine.dispose()
    assert safety.findings(Path(revision.path)) == []


def test_feedback_defaults_and_private_binary_are_separate_from_json(h):
    with transaction(h.app.state.engine, write=True) as db:
        row = report(h, screenshot=IMAGE, screenshot_media_type="image/png")
        db.add(row)
        db.flush()
        report_id = row.id
        assert row.status == "new" and row.occurrences == 1 and row.revision == 1
        assert row.view_state == {} and row.role_names == [] and row.recent_errors == [] and row.request_ids == []
        assert row.recent_routes == []
    with transaction(h.app.state.engine) as db:
        row = db.get(m.FeedbackReport, report_id)
        assert "screenshot" in inspect(row).unloaded
        value = record(row)
        assert "screenshot" not in value and "screenshot_media_type" not in value
        assert "screenshot" in inspect(row).unloaded
        assert row.screenshot == IMAGE


@pytest.mark.parametrize("changes", [
    {"kind": "not-a-kind"}, {"status": "not-a-status"}, {"environment": "test"},
    {"message": ""}, {"message": "x" * (FEEDBACK_LIMITS["message"] + 1)},
    {"expected": "x" * (FEEDBACK_LIMITS["expected"] + 1)},
    {"user_agent": "x" * (FEEDBACK_LIMITS["user_agent"] + 1)}, {"occurrences": 0},
    {"screenshot": IMAGE}, {"screenshot_media_type": "image/png"},
    {"screenshot": IMAGE, "screenshot_media_type": "image/jpeg"},
    {"screenshot": b"x" * (FEEDBACK_LIMITS["screenshot_bytes"] + 1), "screenshot_media_type": "image/png"},
    {"id": "self-link", "duplicate_of": "self-link"},
])
def test_feedback_database_rejects_invalid_values(h, changes):
    with pytest.raises(IntegrityError), transaction(h.app.state.engine, write=True) as db:
        db.add(report(h, **changes))
        db.flush()


def test_worker_clears_only_expired_images_in_bounded_batches(h, monkeypatch):
    engine = h.app.state.engine
    with transaction(engine, write=True) as db:
        rows = [report(h, created_at=m.now() - timedelta(days=days), screenshot=IMAGE,
                       screenshot_media_type="image/png") for days in (181, 180, 179)]
        rows.append(report(h, created_at=m.now() - timedelta(days=365)))
        db.add_all(rows)
        db.flush()
        ids = [row.id for row in rows]
        before = {row.id: record(row) for row in rows}
        audits = db.scalar(select(func.count()).select_from(m.AuditEvent))
    monkeypatch.setattr(worker, "_feedback_pruned", {})
    assert worker.prune_feedback_screenshots(engine, batch=1) == 1
    assert worker.prune_feedback_screenshots(engine) == 0  # throttled between passes
    monkeypatch.setattr(worker, "_feedback_pruned", {})
    assert worker.prune_feedback_screenshots(engine) == 1
    with transaction(engine) as db:
        rows = [db.get(m.FeedbackReport, report_id) for report_id in ids]
        assert all(row is not None for row in rows)
        assert [row.screenshot for row in rows] == [None, None, IMAGE, None]
        assert [row.screenshot_media_type for row in rows] == [None, None, "image/png", None]
        assert {row.id: record(row) for row in rows} == before
        assert db.scalar(select(func.count()).select_from(m.AuditEvent)) == audits


@pytest.mark.postgres
def test_postgres_feedback_bytea_constraints_and_image_retention(pg, monkeypatch):
    engine = pg.app.state.engine
    with transaction(engine, write=True) as db:
        row = report(pg, created_at=m.now() - timedelta(days=180), screenshot=IMAGE,
                     screenshot_media_type="image/png")
        db.add(row)
        db.flush()
        report_id = row.id
        expected = record(row)
    with transaction(engine) as db:
        assert db.get(m.FeedbackReport, report_id).screenshot == IMAGE
    with pytest.raises(IntegrityError), transaction(engine, write=True) as db:
        db.add(report(pg, screenshot=b"x" * (FEEDBACK_LIMITS["screenshot_bytes"] + 1),
                      screenshot_media_type="image/png"))
        db.flush()
    monkeypatch.setattr(worker, "_feedback_pruned", {})
    assert worker.prune_feedback_screenshots(engine, batch=1) == 1
    with transaction(engine) as db:
        row = db.get(m.FeedbackReport, report_id)
        assert row.screenshot is None and row.screenshot_media_type is None
        assert record(row) == expected


def test_feedback_housekeeping_is_called_and_failure_does_not_block_work(h, monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "heartbeat", lambda engine: None)
    monkeypatch.setattr(worker, "prune_security_state", lambda engine, **kwargs: 0)
    def fail(engine):
        calls.append(engine)
        raise RuntimeError("Synthetic housekeeping failure")
    monkeypatch.setattr(worker, "prune_feedback_screenshots", fail)
    assert worker.process_one(h.app.state.engine) is False
    assert calls == [h.app.state.engine]


def test_feedback_and_image_survive_backup_restore_with_count_evidence(h, tmp_path):
    with transaction(h.app.state.engine, write=True) as db:
        row = report(h, screenshot=IMAGE, screenshot_media_type="image/png")
        db.add(row)
        db.flush()
        report_id = row.id
        expected = record(row)
    archive = tmp_path / "feedback-backup.db"
    manifest = backup.backup(h.settings.database_url, archive)
    assert manifest["snapshot"]["counts"]["feedback_reports"] == 1
    restored_path = tmp_path / "feedback-restored.db"
    restored_url = "sqlite:///" + str(restored_path)
    result = restore_drill.drill(archive, restored_url, h.settings.database_url)
    assert result["ok"] and result["head_ok"] and result["tables_ok"]
    assert result["missing_tables"] == [] and result["counts"]["feedback_reports"] == 1
    engine = make_engine(restored_url)
    try:
        with transaction(engine) as db:
            restored = db.get(m.FeedbackReport, report_id)
            assert record(restored) == expected
            assert restored.screenshot == IMAGE and restored.screenshot_media_type == "image/png"
    finally:
        engine.dispose()
    inspected = restore_drill.inspect(restored_url)
    assert inspected["head_ok"] and inspected["counts"]["feedback_reports"] == 1


@pytest.mark.parametrize("role", ["staff", "admin", "auditor"])
def test_feedback_notification_only_reaches_reporter_with_event_status(h, role):
    with transaction(h.app.state.engine, write=True) as db:
        identity_id = h.seed["identities"][role]
        event = m.Outbox(department_id=h.seed["department_id"], event_key="synthetic-feedback:" + role,
                         kind="feedback", payload={"feedback_id": "synthetic-report", "reporter_identity_id": identity_id,
                         "status": "fixed", "resolved_in": "v0.8.1", "actor_id": identity_id})
        db.add(event)
        db.flush()
        worker.deliver_notifications(db, event)
        worker.deliver_notifications(db, event)  # delivery stays idempotent
        notifications = list(db.scalars(select(m.Notification).where(m.Notification.outbox_id == event.id)))
        assert len(notifications) == 1 and notifications[0].identity_id == identity_id
        assert notifications[0].title == "Your feedback was updated"
        assert notifications[0].message == "Your report synthetic-report is fixed in v0.8.1. Open My feedback for the details."


@pytest.mark.parametrize("revoked", ["identity", "binding"])
def test_feedback_notifications_do_not_reach_a_revoked_reporter(h, revoked):
    with transaction(h.app.state.engine, write=True) as db:
        identity_id = h.seed["identities"]["staff"]
        if revoked == "identity":
            db.get(m.Identity, identity_id).disabled = True
        else:
            db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == identity_id)).revoked = True
        event = m.Outbox(department_id=h.seed["department_id"], event_key="synthetic-revoked:" + revoked,
                         kind="feedback", payload={"feedback_id": "synthetic-report", "reporter_identity_id": identity_id,
                                                   "status": "triaged"})
        db.add(event)
        db.flush()
        worker.deliver_notifications(db, event)
        assert db.scalar(select(func.count()).select_from(m.Notification).where(m.Notification.outbox_id == event.id)) == 0
