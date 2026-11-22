"""A whole-rota check's finish writer rebuilds the snapshot only when an input may have moved during the check."""
import copy
import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import delete

from apps.api import models as m, snapshots
from apps.api.db import retry_write, transaction
from apps.worker import main as worker
from tests.clock_support import freeze_clock
from .test_events import event, event_type, repeat
from .test_leave_types import absence


pytestmark = pytest.mark.parametrize(
    'h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)


@pytest.fixture
def clock(monkeypatch):
    now = {'value': datetime(2026, 10, 5, 9, tzinfo=timezone.utc)}
    freeze_clock(monkeypatch, lambda: now['value'])
    return now


@pytest.fixture
def during_finish(monkeypatch):
    """Run `action` after the check computed and before its finish writer starts."""
    def run(action):
        def write(engine, operation, **options):
            if (options.get('context') or {}).get('job_phase') == 'finish':
                action()
            return retry_write(engine, operation, **options)
        monkeypatch.setattr(worker, 'retry_write', write)
    return run


@pytest.fixture
def both_paths(monkeypatch):
    """Settle each check once through the full rebuild (rolled back), then for real; keep both stored results."""
    original, rebuild = worker.finish_job, snapshots.domain_snapshot
    seen = {}

    def stored(db, claim):
        job = db.get(m.Job, claim['id'])
        workspace = db.get(m.Workspace, job.workspace_id)
        return json.dumps([job.status, job.error_code, job.input_fingerprint, job.result, workspace.validation],
                          sort_keys=True, default=str)

    def finish(db, claim, result, error, **options):
        if claim['kind'] != 'validation':
            return original(db, claim, result, error, **options)
        nested = db.begin_nested()
        with monkeypatch.context() as patch:
            patch.setattr(worker, 'check_inputs_unchanged', lambda *args: False)
            original(db, copy.deepcopy(claim), copy.deepcopy(result), error, **options)
            db.flush()
            seen['full'] = stored(db, claim)
        nested.rollback()
        rebuilds = []
        with monkeypatch.context() as patch:
            patch.setattr(snapshots, 'domain_snapshot', lambda *args, **kw: rebuilds.append(1) or rebuild(*args, **kw))
            value = original(db, claim, result, error, **options)
            db.flush()
        seen.update(fast=stored(db, claim), rebuilds=len(rebuilds))
        return value

    monkeypatch.setattr(worker, 'finish_job', finish)
    return seen


def check(h, workspace):
    return h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'validation'},
                     revision=workspace['revision'])['job']


def settle(h, job, seen):
    assert worker.process_one(h.app.state.engine)
    assert seen['fast'] == seen['full']
    # Read the row directly: a test clock moved by days has expired the API sign-in.
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        return {'status': row.status, 'error_code': row.error_code}


def bump(h, workspace, cls, column):
    def change():
        with transaction(h.app.state.engine, write=True) as db:
            row = {m.Organisation: lambda: db.get(m.Organisation, h.seed['organisation_id']),
                   m.Department: lambda: db.get(m.Department, h.seed['department_id']),
                   m.Workspace: lambda: db.get(m.Workspace, workspace['id'])}[cls]()
            setattr(row, column, getattr(row, column) + 1)
    return change


def test_unchanged_check_inputs_are_not_rebuilt_under_the_writer(h, clock, monkeypatch):
    workspace = h.shift(h.workspace())['workspace']
    job = check(h, workspace)
    original, rebuild, rebuilds = worker.finish_job, snapshots.domain_snapshot, []

    def finish(db, claim, *args, **options):
        with monkeypatch.context() as patch:
            patch.setattr(snapshots, 'domain_snapshot', lambda *a, **kw: rebuilds.append(1) or rebuild(*a, **kw))
            return original(db, claim, *args, **options)

    monkeypatch.setattr(worker, 'finish_job', finish)
    assert worker.process_one(h.app.state.engine)
    assert h.get('coordinator', f"/api/jobs/{job['id']}/status")['job']['status'] == 'succeeded'
    assert rebuilds == []


@pytest.mark.parametrize('change', ['unchanged', 'draft edit', 'input change', 'leave'])
def test_finish_stores_the_same_check_as_the_full_rebuild(h, clock, during_finish, both_paths, change):
    workspace = h.shift(h.workspace())['workspace']
    job = check(h, workspace)
    during_finish({
        'unchanged': lambda: None,
        'draft edit': lambda: h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00'),
        'input change': lambda: h.policy({'kind': 'min_rest', 'hours': 24}),
        'leave': lambda: absence(h),
    }[change])
    status = settle(h, job, both_paths)
    # A draft edit is refused by the previous-check comparison on both paths, before any rebuild.
    assert both_paths['rebuilds'] == (0 if change in {'unchanged', 'draft edit'} else 1)
    assert status['status'] == ('succeeded' if change == 'unchanged' else 'failed')
    if status['status'] == 'failed':
        assert status['error_code'] == 'STALE_INPUTS'
    else:
        stored = h.detail(workspace)['workspace']['validation']
        assert stored['input_manifest']['dirty_hint'] == h.snapshot(workspace)['input_manifest']['dirty_hint']


@pytest.mark.parametrize('cls, column', [
    (m.Organisation, 'revision'), (m.Organisation, 'input_revision'), (m.Organisation, 'availability_revision'),
    (m.Department, 'revision'), (m.Department, 'catalogue_revision'),
    (m.Workspace, 'revision'), (m.Workspace, 'content_revision')])
def test_any_moved_check_input_revision_takes_the_full_rebuild(h, clock, during_finish, both_paths, cls, column):
    workspace = h.shift(h.workspace())['workspace']
    job = check(h, workspace)
    during_finish(bump(h, workspace, cls, column))
    settle(h, job, both_paths)
    assert both_paths['rebuilds'] == 1


@pytest.mark.parametrize('change', ['status', 'archived_at'])
def test_a_rota_leaving_draft_during_its_check_is_still_refused(h, clock, during_finish, both_paths, change):
    workspace = h.shift(h.workspace())['workspace']
    job = check(h, workspace)

    def leave_draft():
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Workspace, workspace['id'])
            if change == 'status':
                row.status = 'submitted'
            else:
                row.archived_at = m.now()
    during_finish(leave_draft)
    assert settle(h, job, both_paths)['error_code'] == 'STALE_INPUTS'


def test_a_first_draft_baseline_captured_during_the_check_takes_the_full_rebuild(h, clock, during_finish, both_paths):
    with transaction(h.app.state.engine, write=True) as db:
        actor = h.seed['identities']['coordinator']
        row = m.Workspace(id=m.uid(), created_at=m.now(), department_id=h.seed['department_id'], created_by=actor,
                          last_edited_by=actor, name='Legacy draft without a baseline', starts_on=date(2026, 10, 4),
                          ends_on=date(2026, 10, 11), status='draft', revision=1, content_revision=1)
        db.add(row)
        db.flush()
        workspace = {'id': row.id, 'revision': row.revision}
    job = check(h, workspace)
    during_finish(lambda: h.command('coordinator', 'workspace.baseline.capture', {'reason': 'Capture during check'},
                                    workspace['id'], workspace['revision']))
    settle(h, job, both_paths)
    assert both_paths['rebuilds'] == 1


@pytest.mark.parametrize('moment, later, rebuilds, outcome', [
    # Sunday 09:30 to 10:30 in the event's zone: an occurrence ends during the check.
    ('2026-10-11T06:30:00+00:00', '2026-10-11T07:30:00+00:00', 1, 'failed'),
    # Local midnight passes with no occurrence ending: the projection is unchanged but the date moved.
    ('2026-10-05T09:00:00+00:00', '2026-10-06T09:00:00+00:00', 1, 'succeeded'),
    # The clock moves within one local day and no occurrence ends: the check is current as computed.
    ('2026-10-05T09:00:00+00:00', '2026-10-05T10:00:00+00:00', 0, 'succeeded'),
])
def test_recurring_blocking_events_follow_the_clock_like_the_full_rebuild(
        h, clock, during_finish, both_paths, moment, later, rebuilds, outcome):
    clock['value'] = datetime.fromisoformat(moment)
    event(h, event_type(h, blocks_duties=True, default_audience={'everyone': True}), repeat=repeat())
    with transaction(h.app.state.engine, write=True) as db:
        # Not yet materialised (the daily horizon job lags): every occurrence is projected from the clock.
        db.execute(delete(m.EventOccurrence))
    workspace = h.shift(h.workspace('Recurring event currency', start='2026-10-03', end='2026-10-25'))['workspace']
    job = check(h, workspace)
    during_finish(lambda: clock.update(value=datetime.fromisoformat(later)))
    status = settle(h, job, both_paths)
    assert both_paths['rebuilds'] == rebuilds
    assert status['status'] == outcome
    if outcome == 'failed':
        assert status['error_code'] == 'STALE_INPUTS'
