"""Live incumbents use the existing leased progress and planner permissions."""
import threading
import time
from datetime import timedelta

import pytest
from pydantic import ValidationError

from apps.api import models as m
from apps.api.db import retry_write, transaction
from apps.worker import main as worker
from packages.contracts import schemas as s
from packages.domain.worker_contract import JOB_LEASE_SECONDS
from tests.governance_support import identity


def improvement(seq, shift_id='00000000-0000-0000-0000-000000000001'):
    return {'seq': seq, 'at': m.now().isoformat(), 'kind': 'improvement', 'objective': 100 - seq,
            'bound': 0, 'adds': [{'shift_id': shift_id,
                                 'person_id': '00000000-0000-0000-0000-000000000002'}], 'removes': [],
            'open_places': 1, 'warnings': None, 'truncated': False}


def generation_job(h, workspace=None):
    workspace = workspace or h.workspace()
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
                    revision=workspace['revision'])['job']
    return workspace, job


def test_worker_persists_one_second_progress_ring_and_wakes_live_readers(h, monkeypatch):
    workspace, job = generation_job(h)
    engine = h.app.state.engine
    written = threading.Condition()
    writes, signals, wakes = [], [], []
    heartbeat_job = worker.heartbeat_job
    started = time.monotonic()

    def persist(db, claim, progress):
        alive = heartbeat_job(db, claim, progress)
        with written:
            writes.append((time.monotonic() - started, progress))
            written.notify_all()
        return alive

    monkeypatch.setattr(worker, 'heartbeat_job', persist)
    monkeypatch.setattr(worker.live, 'signal', lambda db, department, **change: signals.append(department))
    monkeypatch.setattr(worker.live, 'changed', lambda department, **change: wakes.append(department))

    def solve(snapshot, *, progress, cancelled):
        assert worker.GENERATION_HEARTBEAT_SECONDS == 1
        assert snapshot['autoschedule_animation'] == s.AutoscheduleAnimationSettings().model_dump()
        for seq in range(1, 66):
            event = improvement(seq)
            progress({'stage': 'improving', 'events': [event], 'improvements_found': seq,
                      'solution': {'seq': seq, 'pairs': event['adds']}})
        progress({'stage': 'improving', 'improvements_found': 70})
        with written:
            assert written.wait_for(lambda: len(writes) >= 1, 5)
        progress({'stage': 'improving', 'events': [improvement(66)], 'improvements_found': 71,
                  'solution': {'seq': 66, 'pairs': improvement(66)['adds']}})
        with written:
            assert written.wait_for(lambda: len(writes) >= 2, 5)
        progress({'stage': 'ready'})
        assert not cancelled()
        return {'status': 'candidate_feasible', 'additions': [], 'removals': []}

    assert worker.process_one(engine, scheduler=solve)
    assert len(writes) == 2
    assert writes[0][0] >= .9 and writes[1][0] - writes[0][0] >= .9
    assert [e['seq'] for e in writes[0][1]['events']] == list(range(16, 66))
    assert writes[0][1]['improvements_found'] == 70
    topic = worker.live.job_topic(h.seed['department_id'], job['id'])
    # The lease, two persisted progress heartbeats, and terminal write each notify
    # the private job stream. SQLite delivers these after commit; this test
    # replaces signal, so there is no direct changed() call to count.
    assert signals == [topic, topic, topic, topic]
    assert not wakes
    with transaction(engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.status == 'succeeded' and row.progress['stage'] == 'ready'
        assert row.progress['improvements_found'] == 71
        assert 'events' not in row.progress and 'solution' not in row.progress
        s.AutoscheduleProgress.model_validate(row.progress)


def test_job_freezes_saved_animation_rate_for_the_existing_solver_path(h):
    engine = h.app.state.engine
    with transaction(engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).display_settings = {
            'autoschedule_animation': {'max_updates_per_second': 8}}
    _, job = generation_job(h)
    with transaction(engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).display_settings = {
            'autoschedule_animation': {'max_updates_per_second': 1}}

    def solve(snapshot, *, progress, cancelled):
        assert snapshot['autoschedule_animation']['max_updates_per_second'] == 8
        return {'status': 'candidate_feasible', 'additions': [], 'removals': []}

    assert worker.process_one(engine, scheduler=solve)


def test_worker_recovery_keeps_monotonic_event_and_improvement_numbers(h, monkeypatch):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    _, job = generation_job(h, workspace)
    shift_id = h.detail(workspace)['shifts'][0]['id']
    first = improvement(1, shift_id)
    engine = h.app.state.engine
    persisted = threading.Event()
    monkeypatch.setattr(worker.live, 'changed', lambda topic, **change: persisted.set() if topic.startswith('generation:') else None)
    retry_write(engine, worker.claim_job)
    with transaction(engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.progress = {'stage': 'improving', 'events': [improvement(30, shift_id)], 'improvements_found': 40}
        row.heartbeat_at = m.now() - timedelta(seconds=JOB_LEASE_SECONDS + 1)

    def solve(snapshot, *, progress, cancelled):
        progress({'stage': 'finding'})
        progress({'stage': 'improving', 'events': [first], 'improvements_found': 2,
                  'solution': {'seq': 1, 'pairs': first['adds']}})
        assert persisted.wait(5)
        detail = h.get('coordinator', '/api/jobs/' + job['id'])['job']
        assert [e['seq'] for e in detail['progress']['events']] == [30, 31]
        assert detail['progress']['events'][-1]['truncated']
        assert detail['progress']['solution'] == {'seq': 31, 'pairs': first['adds']}
        progress({'stage': 'ready'})
        return {'status': 'candidate_feasible', 'additions': [], 'removals': []}

    assert worker.process_one(engine, scheduler=solve)
    detail = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert detail['progress']['improvements_found'] == 42
    assert 'events' not in detail['progress'] and 'solution' not in detail['progress']


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_live_events_require_the_generation_role_on_every_job_read(h, role):
    if role == 'programme_lead':
        identity(h, role, [role])
    workspace = h.shift(h.workspace(), people=[])['workspace']
    _, job = generation_job(h, workspace)
    event = improvement(1, h.detail(workspace)['shifts'][0]['id'])
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.input_snapshot = {**row.input_snapshot, 'focus_ids': [event['adds'][0]['shift_id']]}
        row.status = 'running'
        row.progress = {'stage': 'improving', 'events': [event], 'improvements_found': 1,
                        'solution': {'seq': 1, 'pairs': event['adds']}}
    allowed = role in {'admin', 'coordinator'}
    detail = h.get(role, '/api/jobs/' + job['id'], status=200 if allowed else 403)
    status = h.get(role, '/api/jobs/' + job['id'] + '/status?events=true', status=200 if allowed else 403)
    if allowed:
        assert status['job']['progress']['events'] == [event]
        assert status['job']['result'] is None
    may_list = allowed or role in {'operator', 'approver'}
    listing = h.get(role, '/api/jobs?workspace_id=' + workspace['id'], status=200 if may_list else 403)
    if allowed:
        assert detail['job']['progress']['events'] == [event]
    if may_list:
        assert all('events' not in (row.get('progress') or {}) for row in listing['jobs'])
        assert all('solution' not in (row.get('progress') or {}) for row in listing['jobs'])
    if role == 'operator':
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Job, job['id']).progress['events'] == [event]


@pytest.mark.parametrize('invalid', [
    {'truncated': True},
    {'adds': improvement(1)['adds'] * 201},
    {'adds': improvement(1)['adds'] * 200, 'removes': improvement(1)['adds']},
    {'seq': 0}, {'objective': float('nan')}, {'bound': float('inf')},
])
def test_event_contract_rejects_unbounded_or_invalid_diffs(invalid):
    with pytest.raises(ValidationError):
        s.AutoscheduleLiveEvent.model_validate({**improvement(1), **invalid})


@pytest.mark.parametrize('replay_after_role_loss', [False, True])
def test_operator_cancel_and_receipt_replay_redact_private_progress(h, replay_after_role_loss):
    _, job = generation_job(h)
    event = improvement(1)
    progress = {'stage': 'improving', 'events': [event], 'solution': {'seq': 1, 'pairs': event['adds']}}
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Job, job['id']).progress = progress
    key = 'live-cancel-receipt-privacy'
    if replay_after_role_loss:
        first = h.command('coordinator', 'job.cancel', {'reason': 'Stop this test run'}, target=job['id'], key=key)
        assert first['job']['progress'] == {'stage': 'improving'}
        h.command('operator', 'role.grant', {'identity_id': h.seed['identities']['coordinator'], 'role': 'operator',
                  'starts_on': '2020-01-01', 'reason': 'Synthetic operator-only role'})
        binding = next(row for row in h.get('admin', '/api/access')['bindings']
                       if row['identity_id'] == h.seed['identities']['coordinator'] and row['role'] == 'coordinator')
        h.command('admin', 'role.revoke', {'reason': 'Synthetic role change'}, target=binding['id'], revision=binding['revision'])
    result = h.command('coordinator' if replay_after_role_loss else 'operator', 'job.cancel',
                       {'reason': 'Stop this test run'}, target=job['id'], key=key)
    assert 'events' not in result['job']['progress'] and 'solution' not in result['job']['progress']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job['id']).progress == {'stage': 'improving'}


def test_generation_heartbeat_changes_planner_live_pulse_without_changing_staff_or_inputs(h):
    _, job = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    planner = h.get('coordinator', '/api/pulse')
    staff = h.get('staff', '/api/pulse')
    assert 'generation_progress' not in planner
    event = improvement(1)
    assert retry_write(engine, lambda db: worker.heartbeat_job(db, claim,
        {'stage': 'improving', 'events': [event], 'improvements_found': 1}))
    changed = h.get('coordinator', '/api/pulse')
    assert 'generation_progress' not in changed
    assert changed['activity'] == planner['activity']
    assert changed['input_revision'] == planner['input_revision']
    staff_after = h.get('staff', '/api/pulse')
    assert staff_after['activity'] == staff['activity'] and 'generation_progress' not in staff_after
    stream = h.client('coordinator').get('/api/events?once=true')
    assert stream.status_code == 200 and 'event: pulse' in stream.text
    import json
    payload = json.loads(next(line[6:] for line in stream.text.splitlines() if line.startswith('data: ')))
    assert 'generation_progress' not in payload and 'events' not in payload


def test_progress_contract_preserves_existing_stage_evidence_and_limits_history():
    progress = s.AutoscheduleProgress.model_validate({'stage': 'finding', 'tier': 3, 'model_size': {'variables': 20}})
    assert progress.model_dump()['model_size'] == {'variables': 20}
    with pytest.raises(ValidationError):
        s.AutoscheduleProgress.model_validate({'stage': 'improving', 'events': [improvement(1)] * 51})
