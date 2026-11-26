"""The worker's interactive lane answers short read-only jobs beside a running rota check, and only those."""
import json
import threading
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from apps.api import models as m
from apps.api.db import DatabaseWriteDeferred, transaction
from apps.api.settings import Settings
from apps.api.services import planner_tools, planning, staffing
from apps.worker import main as worker
from packages.domain.worker_contract import JOB_LEASE_SECONDS
from tests.clock_support import freeze_clock

both = pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)

WAIT = 60  # upper bound for a blocked thread; every wait ends on an event, never on time
LANE = worker.INTERACTIVE_KINDS


def drain(h):
    """Run the checks a fixture queued, so each test starts with an empty queue."""
    for _ in range(10):
        if not worker.process_one(h.app.state.engine):
            return
    pytest.fail('queue did not drain')


def queue_check(h, workspace):
    workspace = h.detail(workspace)['workspace']
    return h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'validation'},
                     revision=workspace['revision'])['job']


def queue_fill(h, workspace, shift_id):
    workspace = h.detail(workspace)['workspace']
    response = h.client('coordinator').post('/api/planner/fill-options?async_options=true', json={
        'workspace_id': workspace['id'], 'shift_id': shift_id, 'expected_revision': workspace['revision']})
    assert response.status_code == 202, response.text
    return response.json()['job']


def queue_preview(h, workspace, shift_id):
    workspace = h.detail(workspace)['workspace']
    response = h.client('coordinator').post('/api/planner/preview?async_preview=true', json={
        'kind': 'shift.action', 'expected_revision': workspace['revision'],
        'payload': {'workspace_id': workspace['id'], 'shift_ids': [shift_id], 'action': 'cancel',
                    'reason': 'Interactive lane preview'}})
    assert response.status_code == 202, response.text
    return response.json()['job']


def job(h, job_id):
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job_id)
        return {'status': row.status, 'error_code': row.error_code, 'attempts': row.attempts, 'result': row.result}


def stored_check(h, workspace_id, job_id):
    with transaction(h.app.state.engine) as db:
        return (json.dumps(db.get(m.Workspace, workspace_id).validation, sort_keys=True),
                json.dumps(db.get(m.Job, job_id).result, sort_keys=True))


@pytest.fixture
def rota(h):
    workspace = h.shift(h.workspace('Interactive lane'), people=[])['workspace']
    drain(h)
    return workspace, h.detail(workspace)['shifts'][0]['id']


@pytest.fixture
def held_check(monkeypatch):
    """Hold a running rota check inside its read until the test releases it."""
    started, release = threading.Event(), threading.Event()
    original = planning.revalidate

    def held(*args, **kwargs):
        started.set()
        assert release.wait(WAIT)
        return original(*args, **kwargs)

    monkeypatch.setattr(planning, 'revalidate', held)
    return started, release


def run_main_in_background(h):
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(worker.process_one(h.app.state.engine)), daemon=True)
    thread.start()
    return thread, outcome


@both
def test_lane_answers_fill_options_before_a_running_check_ends_and_leaves_the_check_identical(
        h, rota, held_check, monkeypatch):
    workspace, shift_id = rota
    freeze_clock(monkeypatch)  # a stored check names the time it was made
    started, release = held_check
    release.set()
    # Two plain checks: the second is the reference, with the same stored-check history as the third.
    first = queue_check(h, workspace)
    assert worker.process_one(h.app.state.engine) and job(h, first['id'])['status'] == 'succeeded'
    reference = queue_check(h, workspace)
    assert worker.process_one(h.app.state.engine) and job(h, reference['id'])['status'] == 'succeeded'
    reference_validation, reference_result = stored_check(h, workspace['id'], reference['id'])
    revision_after_reference = h.detail(workspace)['workspace']['revision']

    started.clear()
    release.clear()
    check = queue_check(h, workspace)
    thread, outcome = run_main_in_background(h)
    try:
        assert started.wait(WAIT)
        fill = queue_fill(h, workspace, shift_id)  # queued after the check is running
        assert job(h, check['id'])['status'] == 'running'
        assert worker.process_one(h.app.state.engine, kinds=LANE)
        answered = job(h, fill['id'])
        assert answered['status'] == 'succeeded' and answered['result']['candidates']
        assert job(h, check['id'])['status'] == 'running'  # the candidate list arrived first
    finally:
        release.set()
        thread.join(WAIT)
    assert outcome == [True] and job(h, check['id'])['status'] == 'succeeded'
    assert stored_check(h, workspace['id'], check['id']) == (reference_validation, reference_result)
    assert h.detail(workspace)['workspace']['revision'] == revision_after_reference


@both
def test_lane_never_claims_a_kind_outside_its_allowlist(h, rota):
    workspace, _ = rota
    assert 'validation' not in LANE and LANE == frozenset({'fill_options', 'planner_preview'})
    queued = queue_check(h, workspace)
    assert worker.process_one(h.app.state.engine, kinds=LANE) is False
    assert job(h, queued['id'])['status'] == 'queued'
    lost = m.now() - timedelta(seconds=JOB_LEASE_SECONDS + 5)
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(update(m.Job).where(m.Job.id == queued['id']).values(
            status='running', attempts=1, started_at=lost, heartbeat_at=lost))
    # Lease recovery of other kinds also stays with the main loop.
    assert worker.process_one(h.app.state.engine, kinds=LANE) is False
    assert job(h, queued['id'])['status'] == 'running'
    assert worker.process_one(h.app.state.engine)
    assert job(h, queued['id'])['status'] == 'succeeded'


@both
def test_lane_and_main_loop_never_claim_the_same_job(h, rota):
    workspace, shift_id = rota
    engine = h.app.state.engine.execution_options(rota_defer_busy_writer=False)
    for _round in range(3):
        queued = queue_fill(h, workspace, shift_id)
        barrier, claims = threading.Barrier(2), {}

        def claim(name, **kwargs):
            barrier.wait(WAIT)
            claims[name] = worker.claim_pending(engine, **kwargs)

        threads = [threading.Thread(target=claim, args=('main',)),
                   threading.Thread(target=claim, args=('lane',), kwargs={'kinds': LANE})]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(WAIT)
        taken = [value['id'] for value in claims.values() if value]
        assert sorted(claims) == ['lane', 'main'] and taken == [queued['id']]
        assert job(h, queued['id'])['attempts'] == 1
        with transaction(h.app.state.engine, write=True) as db:
            db.execute(update(m.Job).where(m.Job.id == queued['id']).values(status='succeeded', completed_at=m.now()))


@both
def test_lost_lane_job_is_recovered_by_its_lease(h, rota):
    workspace, shift_id = rota
    queued = queue_fill(h, workspace, shift_id)
    claim = worker.claim_pending(h.app.state.engine, kinds=LANE)
    assert claim['id'] == queued['id']
    lost = m.now() - timedelta(seconds=JOB_LEASE_SECONDS + 5)
    with transaction(h.app.state.engine, write=True) as db:  # the lane died without a heartbeat
        db.execute(update(m.Job).where(m.Job.id == queued['id']).values(started_at=lost, heartbeat_at=lost))
    assert worker.process_one(h.app.state.engine, kinds=LANE)
    recovered = job(h, queued['id'])
    assert recovered['status'] == 'succeeded' and recovered['attempts'] == 2 and recovered['result']['candidates']


@both
def test_lane_preview_records_only_its_receipt(h):
    workspace = h.shift(h.workspace('Interactive lane preview'))['workspace']
    drain(h)
    before = h.detail(workspace)
    queued = queue_preview(h, workspace, before['shifts'][0]['id'])
    assert worker.process_one(h.app.state.engine, kinds=LANE)
    done = job(h, queued['id'])
    assert done['status'] == 'succeeded' and done['result']['committed'] is False
    assert h.detail(workspace) == before


@both
def test_lane_refuses_a_job_that_writes_rota_data_while_it_runs(h, rota, monkeypatch):
    workspace, shift_id = rota
    before = h.detail(workspace)
    original = staffing.fill_options

    def writes(db, actor, value, **kwargs):
        db.get(m.Workspace, value.workspace_id).name = 'Renamed by a candidate check'
        return original(db, actor, value, **kwargs)

    monkeypatch.setattr(staffing, 'fill_options', writes)
    queued = queue_fill(h, workspace, shift_id)
    assert worker.process_one(h.app.state.engine, kinds=LANE)
    refused = job(h, queued['id'])
    assert refused['status'] == 'failed' and refused['error_code'] == 'INTERACTIVE_WRITE_REFUSED'
    assert h.detail(workspace) == before


@both
def test_lane_refuses_a_kind_that_stores_a_check(h, rota):
    workspace, _ = rota
    queued = queue_check(h, workspace)
    before = stored_check(h, workspace['id'], queued['id'])[0]
    # A lane mistakenly allowed to run checks must not store one.
    assert worker.process_one(h.app.state.engine, kinds=frozenset({'validation'}))
    refused = job(h, queued['id'])
    assert refused['status'] == 'failed' and refused['error_code'] == 'INTERACTIVE_WRITE_REFUSED'
    assert stored_check(h, workspace['id'], queued['id'])[0] == before


@both
def test_lane_refuses_feasibility_before_staging_a_proof(h, rota, monkeypatch):
    workspace, _ = rota
    workspace = h.detail(workspace)['workspace']
    queued = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'feasibility'},
                       revision=workspace['revision'])['job']

    def unexpected_check(*args, **kwargs):
        pytest.fail('The interactive lane computed a feasibility proof')

    monkeypatch.setattr('packages.domain.feasibility.check', unexpected_check)
    engine = h.app.state.engine
    assert worker.process_one(engine, kinds=frozenset({'feasibility'}))
    refused = job(h, queued['id'])
    assert refused['status'] == 'failed' and refused['error_code'] == 'INTERACTIVE_WRITE_REFUSED'
    assert refused['result'] is None
    with transaction(engine) as db:
        assert db.scalar(select(m.JobFeasibilityResult.job_id)
                         .where(m.JobFeasibilityResult.job_id == queued['id'])) is None


def test_lane_loop_claims_only_its_kinds_and_waits_for_its_own_wake(monkeypatch):
    stopped, passes, waits = threading.Event(), [], []

    def one_pass(engine, *, settings, kinds):
        passes.append(kinds)
        if len(passes) == 3:
            stopped.set()
        return len(passes) == 1  # work, then idle

    class Wake:
        def wait(self, seconds):
            waits.append(seconds)

        def clear(self):
            pass

    monkeypatch.setattr(worker, 'process_one', one_pass)
    worker.interactive_loop(None, Settings(), stopped, Wake())
    assert passes == [LANE, LANE, LANE]
    assert waits == [Settings().worker_poll_seconds] * 2  # no wait after a pass that worked


@both
def test_lane_never_claims_fill_options_while_a_generation_runs(h, rota):
    workspace, shift_id = rota
    queued = queue_fill(h, workspace, shift_id)
    with transaction(h.app.state.engine, write=True) as db:  # a main-loop generation is running
        fill = db.get(m.Job, queued['id'])
        generation = m.Job(kind='generation', workspace_id=fill.workspace_id, department_id=fill.department_id,
                           requested_by=fill.requested_by, input_fingerprint=fill.input_fingerprint, input_snapshot={},
                           status='running', started_at=m.now(), heartbeat_at=m.now(), attempts=1,
                           worker_id=worker.WORKER_ID)
        db.add(generation)
        db.flush()
        generation_id = generation.id
    assert worker.process_one(h.app.state.engine, kinds=LANE) is False  # one solve at a time
    assert job(h, queued['id'])['status'] == 'queued'
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(update(m.Job).where(m.Job.id == generation_id).values(status='succeeded', completed_at=m.now()))
    assert worker.process_one(h.app.state.engine, kinds=LANE)
    done = job(h, queued['id'])
    assert done['status'] == 'succeeded' and done['attempts'] == 1


@both
def test_lane_refuses_a_nested_write_outside_the_preview_savepoint(h, monkeypatch):
    workspace = h.shift(h.workspace('Interactive lane nested write'))['workspace']
    drain(h)
    before = h.detail(workspace)
    queued = queue_preview(h, workspace, before['shifts'][0]['id'])
    original = planner_tools.preview

    def nested_write(db, actor, command):
        with db.begin_nested():  # a savepoint that is not the preview's own, so it would be committed
            db.get(m.Workspace, workspace['id']).name = 'Renamed in a nested write'
            db.flush()
        return original(db, actor, command)

    monkeypatch.setattr(planner_tools, 'preview', nested_write)
    assert worker.process_one(h.app.state.engine, kinds=LANE)
    refused = job(h, queued['id'])
    assert refused['status'] == 'failed' and refused['error_code'] == 'INTERACTIVE_WRITE_REFUSED'
    assert h.detail(workspace) == before


def lane_running():
    return any(thread.name == 'interactive-lane' for thread in threading.enumerate())


def run_main(h, monkeypatch, *, settle, lane):
    """Run the full worker main(), lane thread included, on the test database for one completed main pass."""
    settings = replace(h.settings, worker_poll_seconds=1)
    monkeypatch.setattr('sys.argv', ['worker'])
    monkeypatch.setattr(worker, 'configure_logging', lambda: None)
    monkeypatch.setattr(worker.Settings, 'from_env', classmethod(lambda cls: settings))
    monkeypatch.setattr(worker, 'make_engine', lambda *args, **kwargs: h.app.state.engine)
    monkeypatch.setattr('apps.worker.score_check.run_loop', lambda *args: None)
    monkeypatch.setattr(worker, 'notification_loop', lambda *args, **kwargs: None)
    original = worker.process_one

    def one_pass(engine, **kwargs):
        worked = original(engine, **kwargs)
        if kwargs.get('kinds') is not None:
            lane(worked)
            return worked
        settle()
        raise SystemExit('stop')

    monkeypatch.setattr(worker, 'process_one', one_pass)
    with pytest.raises(SystemExit, match='stop'):
        worker.main()
    for thread in threading.enumerate():
        if thread.name == 'interactive-lane':
            thread.join(WAIT)
            assert not thread.is_alive()


@both
def test_startup_recovery_never_requeues_a_job_the_lane_claimed(h, rota, monkeypatch):
    """main()'s recovering pass treats every running job of this WORKER_ID as lost; the lane may claim only after it."""
    workspace, shift_id = rota
    queued = queue_fill(h, workspace, shift_id)
    lane_claimed, lane_passed, recovering = threading.Event(), threading.Event(), []
    original_claim = worker.claim_pending

    def claim(engine, **kwargs):
        if kwargs.get('recovering_worker') is None:
            claimed = original_claim(engine, **kwargs)
            if kwargs.get('kinds') is not None:
                lane_claimed.set()
            return claimed
        recovering.append(lane_running())
        if len(recovering) == 1:
            raise DatabaseWriteDeferred()  # a busy writer defers the first recovering pass; recovery stays on
        if lane_running():
            assert lane_claimed.wait(WAIT)  # the order that re-queued a live lane job: the lane claims first
        return original_claim(engine, **kwargs)

    monkeypatch.setattr(worker, 'claim_pending', claim)
    run_main(h, monkeypatch, settle=lambda: lane_passed.wait(WAIT), lane=lambda worked: lane_passed.set())
    assert recovering == [False, False]
    done = job(h, queued['id'])
    assert done['status'] == 'succeeded' and done['attempts'] == 1 and done['error_code'] is None


@both
def test_main_runs_the_lane_beside_a_running_check(h, rota, held_check, monkeypatch):
    workspace, shift_id = rota
    _started, release = held_check
    check = queue_check(h, workspace)
    fill = queue_fill(h, workspace, shift_id)
    answered = []

    def lane(worked):
        if worked:
            answered.append((job(h, fill['id']), job(h, check['id'])['status']))
            release.set()

    run_main(h, monkeypatch, settle=lambda: release.wait(WAIT), lane=lane)
    [(done, check_status)] = answered
    assert done['status'] == 'succeeded' and done['attempts'] == 1 and done['result']['candidates']
    assert check_status == 'running'  # the lane answered while the main loop's check was still running
    checked = job(h, check['id'])
    assert checked['status'] == 'succeeded' and checked['attempts'] == 1
