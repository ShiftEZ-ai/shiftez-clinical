"""Slow pure preview evaluation cannot block real commands or turn a stale preview into authority."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event

import pytest
from sqlalchemy import select

from apps.api import main, models as m
from apps.api.db import transaction
from apps.api.services import catalogue_preview
from .clock_support import freeze_clock
from .test_catalogue_preview import apply, change, preview, state
from .test_events import event, event_type, repeat
from .test_resources_calendars import pool, typ


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)], ids=["sqlite", "postgres"])
def both(request):
    return request.getfixturevalue(request.param)


def setup_preview(h):
    resource = pool(h, capacity=2)
    duty_type = typ(h, resource, resources=[{"resource_pool_id": resource["id"], "units": 2}])
    workspace = h.shift(h.workspace(), typ=duty_type["id"])["workspace"]
    # Sessions exist before concurrent requests; signing in is itself a serialized write.
    h.client("admin")
    h.client("coordinator")
    return resource, workspace, change(resource, capacity=1)


def test_changed_workspace_between_snapshot_and_writer_refuses_preview(both, monkeypatch):
    h = both
    resource, workspace, command = setup_preview(h)
    retry_write = main.retry_write

    def archive_then_acquire(engine, operation, **kwargs):
        archived = h.command("coordinator", "workspace.archive", {"reason": "Concurrent cleanup"},
                             workspace["id"], workspace["revision"])["workspace"]
        assert archived["archived_at"]
        return retry_write(engine, operation, **kwargs)

    monkeypatch.setattr(main, "retry_write", archive_then_acquire)
    result = preview(h, command, status=409)
    assert result["error"]["code"] == "STALE_PREVIEW"
    stored = next(r for r in h.get("admin", "/api/catalogues")["resource_pools"] if r["id"] == resource["id"])
    assert stored["capacity"] == 2


def test_expired_projected_blocking_event_refuses_baseline_on_same_day(both, monkeypatch):
    h = both
    _, _, command = setup_preview(h)
    series = event(h, event_type(h, blocks_duties=True), repeat=repeat(), repeat_until='2026-10-05')
    with transaction(h.app.state.engine, write=True) as db:
        occurrence = db.scalars(select(m.EventOccurrence).where(m.EventOccurrence.event_id == series['id'])).one()
        db.delete(occurrence)  # A missing occurrence is projected until its ending instant.
    now = [datetime(2026, 10, 4, 6, 30, tzinfo=timezone.utc)]
    freeze_clock(monkeypatch, lambda: now[0])
    # Use sessions issued at the pinned clock so the only change is the projected event.
    h.clients.pop('admin').close()
    h.client('admin')
    assert preview(h, command)["impacts"]
    retry_write = main.retry_write

    def advance_clock_then_acquire(engine, operation, **kwargs):
        now[0] = datetime(2026, 10, 4, 7, 1, tzinfo=timezone.utc)
        return retry_write(engine, operation, **kwargs)

    monkeypatch.setattr(main, "retry_write", advance_clock_then_acquire)
    assert preview(h, command, status=409)["error"]["code"] == "STALE_PREVIEW"


def test_commands_progress_while_preview_evaluates_and_old_fingerprint_cannot_apply(both, monkeypatch):
    h = both
    resource, workspace, command = setup_preview(h)
    original, entered, release = catalogue_preview.evaluate, Event(), Event()

    def paused_evaluation(snapshot, **kwargs):
        entered.set()
        assert release.wait(15), "Preview evaluation was not released by the test"
        return original(snapshot, **kwargs)

    monkeypatch.setattr(catalogue_preview, "evaluate", paused_evaluation)
    before = state(h)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(preview, h, command)
        try:
            assert entered.wait(10), "Preview did not reach pure evaluation"
            assert state(h) == before
            stored = next(r for r in h.get("admin", "/api/catalogues")["resource_pools"] if r["id"] == resource["id"])
            assert stored["capacity"] == 2
            archived = executor.submit(h.command, "coordinator", "workspace.archive", {"reason": "Concurrent cleanup"},
                                       workspace["id"], workspace["revision"]).result(timeout=5)["workspace"]
            assert archived["archived_at"] and not release.is_set()
            after_command = state(h)
        finally:
            release.set()
        result = pending.result(timeout=15)
    assert state(h) == after_command
    assert result["before"]["capacity"] == 2 and result["proposed"]["capacity"] == 1
    assert "POOLED_RESOURCE_CAPACITY" in {finding["code"] for finding in result["impacts"][0]["introduced"]}
    assert apply(h, command, result["source_fingerprint"], status=409)["error"]["code"] == "STALE_PREVIEW"
    assert state(h) == after_command


def test_failed_pure_evaluation_keeps_proposal_rolled_back_and_commands_available(both, monkeypatch):
    h = both
    resource, workspace, command = setup_preview(h)
    before = state(h)

    def fail_evaluation(_, **kwargs):
        raise RuntimeError("Synthetic preview evaluator failure")

    monkeypatch.setattr(catalogue_preview, "evaluate", fail_evaluation)
    failed = preview(h, command, status=500)
    assert failed["error"]["code"] == "INTERNAL_ERROR"
    assert state(h) == before
    stored = next(r for r in h.get("admin", "/api/catalogues")["resource_pools"] if r["id"] == resource["id"])
    assert stored["capacity"] == 2
    archived = h.command("coordinator", "workspace.archive", {"reason": "Cleanup after failed preview"},
                         workspace["id"], workspace["revision"])["workspace"]
    assert archived["archived_at"]
