"""SR-25: a timed-out attempt can keep its private model and continue with more time."""
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import retry_write, transaction
from apps.api.services.generation_resume import prune_generation_resume
from apps.worker import main as worker
from packages.contracts.schemas import GenerationOptions
from packages.domain import cp_sat
from packages.domain.solver_control import SolverControl
from packages.domain.solver_resume import prepared_state, resume_key
from tests.test_autoschedule_budget_api import budget_h, exhausted, start


def test_unknown_time_limit_keeps_preparation_through_finish_prune_continue_and_worker(budget_h, monkeypatch):
    h = budget_h
    _, job = start(h, {'search_minutes': 5})
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    options = GenerationOptions.model_validate(claim['input']['generation_options'])
    prepared = cp_sat._prepare(claim['input'], 3, 1, SolverControl(), options, False, False)
    assert 'model' in prepared, prepared
    state = prepared_state(resume_key(claim['input'], options, False, False), prepared)
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim, state))
    result = {'status': 'unknown_time_limit', 'algorithm_version': claim['input']['compiler_version'],
              'input_fingerprint': claim['input']['fingerprint'], 'additions': [], 'removals': []}
    assert retry_write(engine, lambda db: worker.finish_job(db, claim, result, None))
    detail = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    history = h.get('coordinator', '/api/jobs?workspace_id=' + claim['input']['workspace']['id'])['jobs']
    assert detail['status'] == 'succeeded' and detail['outcome'] == 'needs_more_time'
    assert next(row for row in history if row['id'] == job['id'])['outcome'] == 'needs_more_time'
    retry_write(engine, lambda db: prune_generation_resume(db, days=h.settings.generation_resume_days))
    with transaction(engine) as db:
        previous = db.get(m.Job, job['id'])
        snapshot, saved = deepcopy(previous.input_snapshot), deepcopy(previous.resume_state)
        checkpoint = deepcopy(previous.resume_checkpoint)
        assert saved is not None and checkpoint is not None
    continued = h.command('coordinator', 'job.continue',
        {'minutes': 10, 'reason': 'Double the limit after an inconclusive run'}, job['id'])['job']
    assert continued['id'] != job['id'] and continued['status'] == 'queued'
    assert continued['effective_options']['search_minutes'] == 10
    assert continued['search_control'] == {'minutes': 10, 'stop': False}
    assert not {'resume_state', 'resume_checkpoint'}.intersection(continued)
    with transaction(engine) as db:
        previous, current = db.get(m.Job, job['id']), db.get(m.Job, continued['id'])
        assert previous.input_snapshot == snapshot and previous.result == result
        assert previous.resume_state is None and previous.resume_checkpoint is None
        assert current.resume_state == saved and current.resume_checkpoint == checkpoint
        assert current.input_fingerprint == previous.input_fingerprint
        assert current.search_control['spent_seconds'] == 0
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == 'job.continue',
            m.AuditEvent.target_id == job['id'], m.AuditEvent.reason == 'Double the limit after an inconclusive run'))
    monkeypatch.setattr(cp_sat, '_prepare', lambda *args: pytest.fail('Continuation rebuilt its saved model'))
    assert worker.process_one(engine)
    with transaction(engine) as db:
        current = db.get(m.Job, continued['id'])
        assert current.status == 'succeeded' and current.result['status'] == 'candidate_feasible'
        assert current.result['preparation_reused'] and current.result['validation']['valid']


def test_worker_lease_recovery_promotes_checkpoint_candidate_for_review_and_resume(budget_h):
    h = budget_h
    workspace, job = start(h, {'search_minutes': 5})
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    candidate = {'status': 'candidate_with_gaps', 'checkpointed': True,
                 'algorithm_version': claim['input']['compiler_version'],
                 'input_fingerprint': claim['input_fingerprint'], 'additions': [], 'removals': [],
                 'checkpoint_assignments': [['checkpoint-session', 'checkpoint-person']]}
    state = {'key': 'checkpoint-key', 'hint': [1], 'has_solution': True,
             'checkpoint_assignments': candidate['checkpoint_assignments'], 'result': candidate}
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim, state))
    with transaction(engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.heartbeat_at = m.now() - timedelta(seconds=worker.JOB_LEASE_SECONDS + 1)
    retry_write(engine, worker.claim_job)
    with transaction(engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.status == 'succeeded' and row.result['checkpointed']
        assert row.result['validation']['fingerprint']
        assert row.result['status'] in {'candidate_feasible', 'candidate_with_gaps'}
        assert row.resume_checkpoint['checkpoint_assignments'] == candidate['checkpoint_assignments']
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['outcome'] == 'partial'
    continued = h.command('coordinator', 'job.continue',
        {'minutes': 10, 'reason': 'Continue from the best schedule found before the worker stopped'}, job['id'])['job']
    assert continued['status'] == 'queued'
    with transaction(engine) as db:
        row = db.get(m.Job, continued['id'])
        assert row.resume_checkpoint['checkpoint_assignments'] == candidate['checkpoint_assignments']


@pytest.mark.parametrize('status', ['failed', 'cancelled'])
def test_checkpointed_failed_or_cancelled_run_can_continue_without_changing_options(budget_h, status):
    h = budget_h
    _, job = start(h, {'search_minutes': 5, 'target_score': 90})
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    candidate = {'status': 'candidate_with_gaps', 'checkpointed': True,
                 'algorithm_version': claim['input']['compiler_version'],
                 'input_fingerprint': claim['input_fingerprint'], 'additions': [], 'removals': [],
                 'checkpoint_assignments': []}
    state = {'key': 'checkpoint-key', 'hint': [], 'has_solution': True,
             'checkpoint_assignments': [], 'result': candidate}
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim, state))
    with transaction(engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.status, row.completed_at = status, m.now()
        row.result = candidate
    continued = h.command('coordinator', 'job.continue',
                          {'reason': 'Continue the saved checkpoint'}, job['id'])
    assert continued['job']['status'] == 'queued'
    assert continued['job']['effective_options']['target_score'] == 90
    with transaction(engine) as db:
        current = db.get(m.Job, continued['job']['id'])
        assert current.resume_checkpoint is not None
        assert current.input_snapshot['continuation']['from_job_id'] == job['id']


@pytest.mark.parametrize('status', ['candidate_feasible', 'candidate_with_gaps', 'requirements_conflict',
    'invalid_inputs', 'unsupported_rules', 'unsupported_size', 'model_invalid', 'translation_error', 'cancelled', None])
def test_continue_still_refuses_other_result_statuses(budget_h, status):
    h = budget_h
    _, job = start(h)
    exhausted(h, job)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.result = {'status': status}
    reply = h.command('coordinator', 'job.continue', {'minutes': 10, 'reason': 'Refuse a non-continuable outcome'},
                      job['id'], status=409)
    assert reply['error']['code'] == 'JOB_STATE'
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job['id']).result == {'status': status}
        assert not db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == 'job.continue'))


@pytest.mark.parametrize('change,code', [('stale', None), ('limit', 'SEARCH_MINUTES_LIMIT'),
                                      ('compiler', 'STALE_SCHEDULER'), ('role', 'FORBIDDEN')])
def test_unknown_time_limit_preserves_continuation_safety_gates(budget_h, change, code):
    h = budget_h
    workspace, job = start(h)
    exhausted(h, job)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.result = {'status': 'unknown_time_limit', 'algorithm_version': row.input_snapshot['compiler_version']}
        if change == 'stale':
            db.get(m.Workspace, workspace['id']).content_revision += 1
        elif change == 'compiler':
            row.input_snapshot = {**row.input_snapshot, 'compiler_version': 'stale-compiler'}
    reply = h.command('staff' if change == 'role' else 'coordinator', 'job.continue',
        {'minutes': 61 if change == 'limit' else 10, 'reason': 'Retain the existing safety checks'},
        job['id'], status=422 if change == 'limit' else 403 if change == 'role' else 409 if code else 200)
    if code:
        assert reply['error']['code'] == code
    else:
        assert reply['job']['input_fingerprint'] == job['input_fingerprint']
