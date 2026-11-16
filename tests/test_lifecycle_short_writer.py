"""A completed read check never acquires scheduling inputs while holding the writer."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
import uuid
import pytest

from apps.api import models as m, snapshots
from apps.api.db import transaction
from apps.api.services import planning
from tests.test_performance_budget import python_budget
from tests.test_lifecycle_read_performance import command
from tests.test_workspace_check_concurrency import check_harness, paused_check, CHECK_PAYLOAD  # noqa: F401


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
    entered, release = paused_check
    with ThreadPoolExecutor(max_workers=1) as pool:
        check = pool.submit(h.command, 'coordinator', 'workspace.validate', CHECK_PAYLOAD,
            workspace['id'], workspace['revision'], status=409)
        try:
            assert entered.wait(10)
            with transaction(h.app.state.engine, write=True) as db:
                db.get(m.Workspace, workspace['id']).revision += 1
        finally:
            release.set()
        assert check.result(timeout=30)['error']['code'] == 'STALE_REVISION'
