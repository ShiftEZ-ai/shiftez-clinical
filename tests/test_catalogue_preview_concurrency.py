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
from .test_contact_preview_perf import change as person_change, person
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
    command['expected_fingerprint'] = preview(h, command)['source_fingerprint']
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


@pytest.mark.parametrize('concurrent_writes', [1, 2])
def test_unbound_preview_recaptures_once(both, monkeypatch, caplog, concurrent_writes):
    h = both
    _, workspace, command = setup_preview(h)
    workspaces = [workspace, h.workspace('Second concurrent rota')]
    retry_write = main.retry_write
    admissions = []

    def archive_then_acquire(engine, operation, **kwargs):
        index = len(admissions)
        admissions.append(index)
        if index < concurrent_writes:
            row = workspaces[index]
            h.command('coordinator', 'workspace.archive', {'reason': 'Concurrent cleanup'}, row['id'], row['revision'])
        return retry_write(engine, operation, **kwargs)

    monkeypatch.setattr(main, 'retry_write', archive_then_acquire)
    with caplog.at_level('INFO', logger='rota.api'):
        result = preview(h, command, status=200 if concurrent_writes == 1 else 409)

    assert admissions == [0, 1]
    assert [rec.message for rec in caplog.records].count('catalogue_preview_recapture') == 1
    if concurrent_writes == 1:
        assert [impact['workspace_id'] for impact in result['impacts']] == [workspaces[1]['id']]
    else:
        assert result['error']['code'] == 'STALE_PREVIEW'


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
    original_preview = preview(h, command)
    assert original_preview["impacts"]
    command['expected_fingerprint'] = original_preview['source_fingerprint']
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


def test_other_workspace_save_completes_during_proposed_person_snapshot_build(both, monkeypatch):
    h = both
    first = h.shift(h.workspace())['workspace']
    other = h.workspace('Another rota')
    row = person(h)
    command = person_change(row, active=False)
    original, entered, release = catalogue_preview.domain_snapshot, Event(), Event()

    def build(db, workspace, **kwargs):
        if kwargs.get('operational') and '_event_currency' not in kwargs and not entered.is_set():
            entered.set()
            assert release.wait(15)
        return original(db, workspace, **kwargs)

    monkeypatch.setattr(catalogue_preview, 'domain_snapshot', build)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(preview, h, command, 'coordinator')
        try:
            assert entered.wait(15)
            saved = executor.submit(h.shift, other, people=[]).result(timeout=8)
            assert saved['workspace']['revision'] > other['revision']
        finally:
            release.set()
        result = pending.result(timeout=15)
    impact = next(item for item in result['impacts'] if item['workspace_id'] == first['id'])
    assert impact['after_error_count'] > impact['before_error_count']
    assert person(h) == row
    assert apply(h, command, result['source_fingerprint'], status=409)['error']['code'] == 'STALE_PREVIEW'
