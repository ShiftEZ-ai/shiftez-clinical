"""A completed read check never acquires scheduling inputs while holding the writer."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
import uuid
import pytest
from sqlalchemy import select

from apps.api import models as m, snapshots
from apps.api.db import transaction
from apps.api.services import planning
from tests.test_performance_budget import python_budget
from tests.test_lifecycle_read_performance import command
from tests.test_workspace_check_concurrency import (  # noqa: F401
    check_harness, paused_check, CHECK_PAYLOAD, stored_check, assert_no_receipt)


def forbid_writer_snapshots(monkeypatch):
    original, calls = snapshots._domain_snapshot, []
    def checked(db, *args, **kwargs):
        assert not db.connection().info.get('rota_write'), 'Lifecycle freshness must not rebuild history under the writer'
        calls.append(1)
        return original(db, *args, **kwargs)
    monkeypatch.setattr(snapshots, '_domain_snapshot', checked)
    return calls


def test_approval_finishes_while_same_rotas_check_is_paused(check_harness, monkeypatch):
    h = check_harness
    workspace = h.shift(h.checked_workspace)['workspace']
    workspace = h.lifecycle(workspace, 'submit')['workspace']
    forbid_writer_snapshots(monkeypatch)
    entered, release = Event(), Event()
    evaluate = planning.evaluate
    def held(data, **options):
        if not entered.is_set():
            entered.set()
            assert release.wait(30)
        return evaluate(data, **options)
    monkeypatch.setattr(planning, 'evaluate', held)
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        check = pool.submit(h.command, 'coordinator', 'workspace.validate', CHECK_PAYLOAD,
            workspace['id'], workspace['revision'], key=key, status=409)
        try:
            assert entered.wait(10)
            start = time.perf_counter()
            approval = pool.submit(h.lifecycle, workspace, 'approve', 'approver')
            assert approval.result(timeout=python_budget(5))['workspace']['status'] == 'approved'
            elapsed = time.perf_counter() - start
            assert not check.done()
        finally:
            release.set()
        assert check.result(timeout=30)['error']['code'] == 'STALE_INPUTS'
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Workspace, workspace['id']).status == 'approved'
    return locals()


@pytest.mark.wall_benchmark
def test_approval_finishes_within_short_writer_budget(check_harness, monkeypatch):
    state = test_approval_finishes_while_same_rotas_check_is_paused(check_harness, monkeypatch)
    assert state['elapsed'] < python_budget(5)


def test_unrelated_input_revision_is_resolved_after_releasing_writer(check_harness, paused_check, monkeypatch):
    h, workspace = check_harness, check_harness.checked_workspace
    calls = forbid_writer_snapshots(monkeypatch)
    entered, release = paused_check
    with ThreadPoolExecutor(max_workers=1) as pool:
        # Measure the command itself, not Harness.command's additional detail hydration.
        check = pool.submit(command, h, workspace, 'validate')
        try:
            assert entered.wait(10)
            with transaction(h.app.state.engine, write=True) as db:
                db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
        finally:
            release.set()
        assert check.result(timeout=30)['workspace']['revision'] == workspace['revision']
    assert len(calls) == 2, 'One coherent preparation and one canonical drift check, both outside the writer'


def test_revision_only_race_is_checked_inside_final_writer(check_harness, paused_check):
    h, workspace = check_harness, check_harness.checked_workspace
    before = stored_check(h, workspace)
    entered, release = paused_check
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        check = pool.submit(h.command, 'coordinator', 'workspace.validate', CHECK_PAYLOAD,
            workspace['id'], workspace['revision'], key=key)
        try:
            assert entered.wait(10)
            with transaction(h.app.state.engine, write=True) as db:
                db.get(m.Workspace, workspace['id']).revision += 1
        finally:
            release.set()
        result = check.result(timeout=30)
    assert result['workspace']['revision'] == workspace['revision'] + 1
    assert result['workspace']['content_revision'] == workspace['content_revision']
    assert result['validation']['fingerprint'] == before['fingerprint']
    assert result['validation']['evaluated_at'] != before['evaluated_at']
    assert stored_check(h, workspace) == result['validation']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Workspace, workspace['id']).revision == result['workspace']['revision']
        receipt = db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.key == key))
        assert receipt.result['workspace']['revision'] == result['workspace']['revision']
        assert receipt.result['validation']['evaluated_at'] == result['validation']['evaluated_at']


def test_content_change_race_retains_completed_check_without_storing_it(check_harness, paused_check):
    h, workspace = check_harness, check_harness.checked_workspace
    before = stored_check(h, workspace)
    entered, release = paused_check
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        check = pool.submit(h.command, 'coordinator', 'workspace.validate', CHECK_PAYLOAD,
            workspace['id'], workspace['revision'], key=key, status=409)
        try:
            assert entered.wait(10)
            with transaction(h.app.state.engine, write=True) as db:
                current = db.get(m.Workspace, workspace['id'])
                current.name = 'Changed during the held check'
                current.content_revision += 1
                current.revision += 1
        finally:
            release.set()
        error = check.result(timeout=30)['error']
    assert error['code'] == 'STALE_INPUTS'
    details = error['details']
    assert details['recoverable'] is True
    assert details['current_revision'] == workspace['revision'] + 1
    assert details['current_fingerprint'] != before['fingerprint']
    assert details['validation']['fingerprint'] == before['fingerprint']
    assert details['validation']['evaluated_at'] != before['evaluated_at']
    assert details['validation']['findings'] == before['findings']
    assert stored_check(h, workspace) == before
    assert_no_receipt(h, key)
    with transaction(h.app.state.engine) as db:
        current = db.get(m.Workspace, workspace['id'])
        assert current.name == 'Changed during the held check'
        assert current.content_revision == workspace['content_revision'] + 1
