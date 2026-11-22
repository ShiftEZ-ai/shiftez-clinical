"""The worker's interactive lane answers short read-only jobs beside a running rota check, and only those."""
import json
import threading
from datetime import timedelta

import pytest
from sqlalchemy import update

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning, staffing
from apps.worker import main as worker
from packages.domain.worker_contract import JOB_LEASE_SECONDS
from tests.clock_support import freeze_clock

pytestmark = pytest.mark.parametrize(
    'h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)

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


def test_lane_preview_records_only_its_receipt(h):
    workspace = h.shift(h.workspace('Interactive lane preview'))['workspace']
    drain(h)
    before = h.detail(workspace)
    queued = queue_preview(h, workspace, before['shifts'][0]['id'])
    assert worker.process_one(h.app.state.engine, kinds=LANE)
    done = job(h, queued['id'])
    assert done['status'] == 'succeeded' and done['result']['committed'] is False
    assert h.detail(workspace) == before


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


def test_lane_refuses_a_kind_that_stores_a_check(h, rota):
    workspace, _ = rota
    queued = queue_check(h, workspace)
    before = stored_check(h, workspace['id'], queued['id'])[0]
    # A lane mistakenly allowed to run checks must not store one.
    assert worker.process_one(h.app.state.engine, kinds=frozenset({'validation'}))
    refused = job(h, queued['id'])
    assert refused['status'] == 'failed' and refused['error_code'] == 'INTERACTIVE_WRITE_REFUSED'
    assert stored_check(h, workspace['id'], queued['id'])[0] == before
