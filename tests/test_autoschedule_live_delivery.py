"""Transient run payloads and wakeups stay on the authorized active-run path."""
import asyncio
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest

from apps.api import live, models as m
from apps.api.commands import receipt_result
from apps.api.db import retry_write, transaction
from apps.api.serialization import job_progress_record, job_record
from apps.worker import main as worker
from packages.domain.solver_rules import COMPILER_VERSION
from tests.test_autoschedule_live_worker import generation_job, improvement


def live_progress(shift_id=None):
    event = improvement(1)
    if shift_id is not None:
        event['adds'][0]['shift_id'] = shift_id
    return {'stage': 'improving', 'elapsed_seconds': 9, 'improvements_found': 13,
            'events': [event], 'solution': {'seq': 1, 'pairs': event['adds']}}


@pytest.mark.parametrize('terminal', ['success', 'failure', 'stored', 'cancel', 'expired', 'recovered', 'rejected'])
def test_terminal_jobs_drop_live_payload_and_keep_summary(h, terminal):
    _, job = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job) if terminal != 'rejected' else None
    progress = live_progress()
    summary = job_progress_record(progress)
    with transaction(engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.progress = progress
        if terminal in {'expired', 'recovered'}:
            row.attempts = 3
            row.heartbeat_at = m.now() - timedelta(seconds=worker.JOB_LEASE_SECONDS + 1)
            if terminal == 'recovered':
                row.input_snapshot = {**row.input_snapshot, 'generation_engine': 'cp_sat', 'compiler_version': COMPILER_VERSION}
                row.result = {'status': 'candidate_feasible', 'input_fingerprint': row.input_fingerprint}
            else:
                row.input_snapshot = {**row.input_snapshot, 'generation_engine': 'legacy'}
        elif terminal == 'rejected':
            db.get(m.Identity, row.requested_by).disabled = True
    if terminal == 'cancel':
        h.command('coordinator', 'job.cancel', {'reason': 'Stop synthetic run'}, target=job['id'])
    elif terminal in {'expired', 'recovered', 'rejected'}:
        assert retry_write(engine, worker.claim_job) is None
    else:
        assert retry_write(engine, lambda db: worker.finish_job(db, claim,
            {'status': 'candidate_feasible', 'additions': [], 'removals': []},
            'SyntheticFailure' if terminal == 'failure' else None,
            progress=None if terminal == 'stored' else progress))
    with transaction(engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.status in {'succeeded', 'failed', 'cancelled'}
        assert row.progress == summary


@pytest.mark.parametrize('status', ['queued', 'running', 'succeeded', 'failed', 'cancelled', 'applied'])
def test_only_explicit_running_job_detail_exposes_live_payload(h, status):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    _, job = generation_job(h, workspace)
    shift_id = h.detail(workspace)['shifts'][0]['id']
    progress = live_progress(shift_id)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.input_snapshot = {**row.input_snapshot, 'focus_ids': [shift_id]}
        row.status, row.progress = status, progress
        assert job_record(row)['progress'] == job_progress_record(progress)
        assert job_record(row, include_events=True)['progress'] == progress
    listing = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])
    assert listing['jobs'][0]['progress'] == job_progress_record(progress)
    detail = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert detail['progress'] == (progress if status == 'running' else job_progress_record(progress))


def test_historical_receipt_payload_is_compact_even_for_planners():
    progress = live_progress()
    original = {'job': {'progress': progress}}
    result = receipt_result('job.cancel', original, actor=SimpleNamespace(permits=lambda *roles: True))
    assert result['job']['progress'] == job_progress_record(progress)
    assert original['job']['progress'] == progress


@pytest.mark.parametrize('kind', ['generation', 'fill_options', 'validation', 'feasibility', 'catalogue_proof', 'swap_audience'])
def test_only_changed_generation_progress_emits_heartbeat_notifications(h, monkeypatch, kind):
    workspace, job = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    with transaction(engine, write=True) as db:
        db.get(m.Job, job['id']).kind = kind
    notified = []
    monkeypatch.setattr(live, 'signal', lambda db, topic, **change: notified.append(topic))
    progress = live_progress()
    assert retry_write(engine, lambda db: worker.heartbeat_job(db, claim, progress))
    with transaction(engine) as db:
        revision = db.get(m.Job, job['id']).revision
    assert retry_write(engine, lambda db: worker.heartbeat_job(db, claim, progress))
    with transaction(engine) as db:
        assert db.get(m.Job, job['id']).revision == revision
    assert notified == ([live.job_topic(h.seed['department_id'], job['id'])]
                        if kind in {'generation', 'fill_options'} else [])
    assert retry_write(engine, lambda db: worker.heartbeat_job(db, claim, {**progress, 'elapsed_seconds': 10}))
    with transaction(engine) as db:
        expected = revision + 1 if kind in {'generation', 'fill_options'} else revision
        assert db.get(m.Job, job['id']).revision == expected
    progress = {**progress, 'events': [improvement(2)]}
    assert retry_write(engine, lambda db: worker.heartbeat_job(db, claim, progress))
    progress = {**progress, 'stage': 'validating'}
    assert retry_write(engine, lambda db: worker.heartbeat_job(db, claim, progress))
    assert notified == ([live.job_topic(h.seed['department_id'], job['id'])] * 4
                        if kind in {'generation', 'fill_options'} else [])


def test_generation_progress_delivers_phases_score_and_bounded_clock_ticks(h, monkeypatch):
    _, job = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    notified = []
    monkeypatch.setattr(live, 'signal', lambda db, topic, **change: notified.append(topic))
    score = {'total': 72.5, 'components': {'coverage': 1, 'rules': 0.8, 'fairness': None,
                                           'preferences': None, 'stability': None}}
    for update in ({'stage': 'preparing', 'elapsed_seconds': 0},
                   {'stage': 'searching', 'elapsed_seconds': 1},
                   {'stage': 'improving', 'elapsed_seconds': 2, 'best_score': score},
                   {'stage': 'improving', 'elapsed_seconds': 2.5, 'best_score': score}):
        assert retry_write(engine, lambda db, update=update: worker.heartbeat_job(db, claim, update))
    topic = live.job_topic(h.seed['department_id'], job['id'])
    assert notified == [topic] * 3
    with transaction(engine) as db:
        progress = db.get(m.Job, job['id']).progress
        assert progress['stage'] == 'improving' and progress['best_score'] == score


@pytest.mark.parametrize('kind,seconds', [('generation', 1), ('validation', 3)])
def test_lease_monitor_uses_generation_only_one_second_cadence(h, monkeypatch, kind, seconds):
    workspace = h.workspace()
    h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': kind}, revision=workspace['revision'])
    waits = []

    class Finished(threading.Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            return True

    monkeypatch.setattr(worker, 'threading', SimpleNamespace(Event=Finished, Thread=threading.Thread, Lock=threading.Lock))
    assert worker.process_one(h.app.state.engine, scheduler=lambda *args, **kwargs: {'status': 'candidate_feasible'})
    assert waits == [seconds]


def test_generation_wakes_only_authorized_department_and_matching_rota_subscribers():
    async def scenario():
        live.attach(asyncio.get_running_loop())
        staff = live.subscription_topics({'department_id': 'department', 'roles': ['staff']})
        planner = live.subscription_topics({'department_id': 'department', 'roles': ['coordinator']})
        rota = live.subscription_topics({'department_id': 'department', 'roles': ['coordinator']}, 'rota')
        job = live.subscription_topics({'department_id': 'department', 'roles': ['coordinator']}, job_ids=('job-a',))
        other = live.subscription_topics({'department_id': 'department', 'roles': ['coordinator']}, 'other')
        foreign = live.subscription_topics({'department_id': 'foreign', 'roles': ['coordinator']})
        topics = [staff, planner, rota, job, other, foreign]
        tasks = [asyncio.create_task(live.wait(value, live.version(value), 5)) for value in topics]
        try:
            ready = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(ready.set_result, None)
            await ready
            live.changed(live.generation_topic('department', 'rota'))
            await asyncio.wait_for(asyncio.gather(tasks[1], tasks[2]), 1)
            assert not tasks[0].done() and not tasks[3].done() and not tasks[4].done() and not tasks[5].done()
            assert live.version('department') == live.version('foreign') == 0
            live.changed(live.job_topic('department', 'job-a'), scope='jobs', id='job-a')
            await asyncio.wait_for(tasks[3], 1)
            assert not tasks[0].done() and not tasks[4].done() and not tasks[5].done()
            assert live.subscription_topics({'department_id': 'department', 'roles': ['staff']}) == staff
            live.changed('department')
            await asyncio.wait_for(asyncio.gather(tasks[0], tasks[4]), 1)
            assert not tasks[5].done()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            live.detach(asyncio.get_running_loop())
    asyncio.run(scenario())


def test_rota_subscriptions_require_planner_access_and_department_scope(h):
    workspace, _ = generation_job(h)
    with transaction(h.app.state.engine, write=True) as db:
        original = db.get(m.Workspace, workspace['id'])
        department = m.Department(organisation_id=db.get(m.Department, h.seed['department_id']).organisation_id,
                                  name='Other synthetic department')
        db.add(department)
        db.flush()
        foreign = m.Workspace(department_id=department.id, name='Other synthetic rota',
                              starts_on=original.starts_on, ends_on=original.ends_on,
                              created_by=original.created_by, last_edited_by=original.last_edited_by)
        db.add(foreign)
        db.flush()
        foreign_id = foreign.id
    for endpoint in ('/api/events?once=true&workspace_id=', '/api/pulse?workspace_id='):
        h.get('staff', endpoint + workspace['id'], status=403)
        h.get('operator', endpoint + workspace['id'], status=403)
        h.get('coordinator', endpoint + '00000000-0000-0000-0000-000000000001', status=404)
        h.get('coordinator', endpoint + foreign_id, status=404)
    assert h.client('coordinator').get('/api/events?once=true&workspace_id=' + workspace['id']).status_code == 200
