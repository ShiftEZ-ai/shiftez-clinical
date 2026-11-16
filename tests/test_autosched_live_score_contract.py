"""Live scores use canonical ranges and survive progress updates without private score details."""
import copy
from datetime import timedelta

import pytest
from pydantic import ValidationError

from apps.api import models as m
from apps.api.db import retry_write, transaction
from apps.api.serialization import job_progress_record
from apps.worker import main as worker
from packages.contracts import schemas as s
from tests.test_autoschedule_live_worker import generation_job


SCORE = {'total': 59.0, 'components': {'coverage': 0.8, 'rules': 0.9, 'fairness': 1.0,
                                    'preferences': 0.0, 'stability': None}}


def test_live_score_is_unknown_until_provided_and_keeps_canonical_units():
    assert s.AutoscheduleProgress(stage='finding').best_score is None
    score = s.AutoscheduleProgress(stage='improving', best_score=SCORE).model_dump()['best_score']
    assert score == SCORE
    assert job_progress_record({'stage': 'finding'}) == {'stage': 'finding'}
    for include_events in (False, True):
        assert job_progress_record({'stage': 'improving', 'best_score': score},
                                   include_events=include_events)['best_score'] == SCORE


@pytest.mark.parametrize('field,value', [
    ('total', -0.1), ('total', 100.1), ('total', float('nan')), ('total', float('inf')), ('total', True),
    ('coverage', -0.1), ('coverage', 1.1), ('coverage', float('nan')), ('coverage', float('inf')),
])
def test_live_score_contract_rejects_nonfinite_or_out_of_range_values(field, value):
    score = copy.deepcopy(SCORE)
    (score if field == 'total' else score['components'])[field] = value
    with pytest.raises(ValidationError):
        s.AutoscheduleProgress(stage='improving', best_score=score)


@pytest.mark.parametrize('change', ['missing_component', 'private_top_level', 'private_component'])
def test_live_score_contract_requires_all_components_and_forbids_private_details(change):
    score = copy.deepcopy(SCORE)
    if change == 'missing_component':
        del score['components']['stability']
    elif change == 'private_top_level':
        score['people'] = {'private-person': {'score': 99}}
    else:
        score['components']['person_id'] = 'private-person'
    with pytest.raises(ValidationError):
        s.AutoscheduleProgress(stage='improving', best_score=score)


@pytest.mark.parametrize('clear_score', [False, True])
def test_worker_retains_score_across_heartbeats_recovery_and_stage_updates(h, monkeypatch, clear_score):
    workspace, job = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    signals = []
    monkeypatch.setattr(worker.live, 'signal', lambda db, topic, **change: signals.append(topic))
    assert retry_write(engine, lambda db: worker.heartbeat_job(db, claim,
        {'stage': 'improving', 'best_score': SCORE}))
    # 60315a5de made running-job progress private; the web client subscribes by job id.
    assert signals == [worker.live.job_topic(h.seed['department_id'], job['id'])]
    signals.clear()
    assert retry_write(engine, lambda db: worker.heartbeat_job(db, claim, {'stage': 'improving'}))
    assert not signals
    with transaction(engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        assert row.progress['best_score'] == SCORE
        row.heartbeat_at = m.now() - timedelta(seconds=worker.JOB_LEASE_SECONDS + 1)
    better = {**SCORE, 'total': 60.0}
    recovered = []
    claim_job = worker.claim_job

    def reclaim(db, **kwargs):
        claim = claim_job(db, **kwargs)
        recovered.append(claim['progress']['best_score'])
        return claim

    def solve(snapshot, *, progress, cancelled):
        progress({'stage': 'checking'})
        progress({'stage': 'improving', 'best_score': better})
        progress({'stage': 'improving', **({'best_score': None} if clear_score else {})})
        progress({'stage': 'ready'})
        return {'status': 'candidate_feasible', 'additions': [], 'removals': []}

    monkeypatch.setattr(worker, 'claim_job', reclaim)
    assert worker.process_one(engine, scheduler=solve)
    assert recovered == [SCORE]
    detail = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    listed = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs']
    expected = None if clear_score else better
    assert detail['progress']['best_score'] == expected
    assert next(row for row in listed if row['id'] == job['id'])['progress']['best_score'] == expected
