"""The worker and planner settle path must keep a check bound to the revision it checked."""
from datetime import datetime, timezone

import pytest

from apps.api import models as m
from apps.api.db import retry_write, transaction
from apps.worker import main as worker
from tests.clock_support import freeze_clock
from tests.test_p2_groups import fork
from tests.test_planning_groups_drafts import settings


pytestmark = pytest.mark.parametrize(
    'h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)


@pytest.fixture
def queued_check(h, monkeypatch):
    freeze_clock(monkeypatch, datetime(2026, 10, 5, tzinfo=timezone.utc))
    workspace = h.shift(h.workspace(), people=[])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'validation'},
                    revision=workspace['revision'])['job']
    return workspace, job


@pytest.fixture
def before_check_finishes(monkeypatch):
    def run(action):
        def write(engine, operation, **options):
            if (options.get('context') or {}).get('job_phase') == 'finish':
                action()
            return retry_write(engine, operation, **options)
        monkeypatch.setattr(worker, 'retry_write', write)
    return run


@pytest.mark.parametrize('action', ['share', 'designate'])
def test_running_check_lands_after_draft_access_changes(h, action, before_check_finishes):
    if action == 'designate':
        settings(h)
    workspace = h.shift(h.workspace(), people=[])['workspace']
    if action == 'share':
        workspace = fork(h, workspace)
        # A private fork starts with a frozen check and no stored validation.
        job = h.get('coordinator', f"/api/jobs?workspace_id={workspace['id']}")['jobs'][0]
        assert job['kind'] == 'validation' and job['status'] == 'queued'
    else:
        job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'validation'},
                        revision=workspace['revision'])['job']
    path = f"/api/jobs/{job['id']}/status"
    inputs = h.snapshot(workspace)['fingerprint']
    changed = None

    def change_access():
        nonlocal changed
        assert h.get('coordinator', path)['job']['status'] == 'running'
        changed = h.command('coordinator' if action == 'share' else 'admin', f'workspace.{action}',
                            {'reason': 'Update draft access'}, workspace['id'], workspace['revision'])['workspace']

    before_check_finishes(change_access)
    assert worker.process_one(h.app.state.engine)
    assert changed['revision'] == workspace['revision'] + 1
    assert changed['content_revision'] == workspace['content_revision']
    status = h.get('coordinator', path)['job']
    assert status['status'] == 'succeeded'
    assert status['delta']['revision'] == changed['revision'] and status['delta']['stale'] is False
    assert status['delta']['current_fingerprint'] == inputs
    assert h.detail(changed)['workspace']['validation']['fingerprint'] == inputs
    jobs = h.get('coordinator', f"/api/jobs?workspace_id={workspace['id']}")['jobs']
    assert [row['id'] for row in jobs] == [job['id']]


def test_completed_check_keeps_its_revision_after_a_newer_edit(h, queued_check):
    workspace, job = queued_check
    assert worker.process_one(h.app.state.engine)
    path = f"/api/jobs/{job['id']}/status"
    delta = h.get('coordinator', path)['job']['delta']
    assert delta['kind'] == 'complete_check' and delta['revision'] == workspace['revision']
    assert delta['stale'] is False and not delta['partial']
    assert delta['current_fingerprint'] == delta['validation']['fingerprint']

    newer = h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    late = h.get('coordinator', path)['job']['delta']
    assert late == {**delta, 'stale': True}
    detail = h.detail(newer)['workspace']
    assert detail['revision'] == newer['revision'] > delta['revision']
    assert detail['validation']['fingerprint'] != delta['current_fingerprint']
    for endpoint in ('/api/workspaces', '/api/home/rotas?start=2026-10-04&end=2026-10-11'):
        row = next(row for row in h.get('coordinator', endpoint)['workspaces'] if row['id'] == newer['id'])
        assert row['health']['fresh'] is True
        assert row['health']['summary']['places_filled'] == 1


def test_completed_check_is_stale_when_inputs_change_without_a_draft_edit(h, queued_check):
    workspace, job = queued_check
    assert worker.process_one(h.app.state.engine)
    h.policy({'kind': 'min_rest', 'hours': 24})
    delta = h.get('coordinator', f"/api/jobs/{job['id']}/status")['job']['delta']
    assert delta['revision'] == workspace['revision'] and delta['stale'] is True
    for endpoint in ('/api/workspaces', '/api/home/rotas?start=2026-10-04&end=2026-10-11'):
        row = next(row for row in h.get('coordinator', endpoint)['workspaces'] if row['id'] == workspace['id'])
        assert row['health']['fresh'] is False


def test_late_worker_cannot_replace_newer_evidence_at_the_same_input_fingerprint(h, queued_check, before_check_finishes):
    workspace, job = queued_check
    engine = h.app.state.engine
    newer = None

    def replace_evidence():
        nonlocal newer
        with transaction(engine, write=True) as db:
            row = db.get(m.Workspace, workspace['id'])
            row.name = 'Updated draft'
            row.revision += 1
            newer = {**row.validation, 'evaluated_at': '2026-10-05T12:00:00+00:00'}
            row.validation = newer

    before_check_finishes(replace_evidence)
    assert worker.process_one(engine)
    status = h.get('coordinator', f"/api/jobs/{job['id']}/status")['job']
    assert status['status'] == 'failed' and status['error_code'] == 'STALE_INPUTS'
    assert status['delta'] is None
    with transaction(engine) as db:
        assert db.get(m.Workspace, workspace['id']).validation == newer


def test_unstamped_historical_job_does_not_supply_a_delta(h, queued_check):
    _, job = queued_check
    engine = h.app.state.engine
    assert worker.process_one(engine)
    with transaction(engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        projection = row.result_summary
        row.result_summary = {**projection, 'result': {
            key: value for key, value in projection['result'].items() if key != 'checked_revision'}}
    status = h.get('coordinator', f"/api/jobs/{job['id']}/status")['job']
    assert status['status'] == 'succeeded' and status['delta'] is None
