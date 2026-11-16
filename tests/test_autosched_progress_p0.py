"""Early checks remain bounded, private and visible while scheduling continues."""
import copy
from datetime import timedelta

import pytest
from pydantic import ValidationError

from apps.api import models as m
from apps.api.db import retry_write, transaction
from apps.api.serialization import job_progress_record
from apps.worker import main as worker
from packages.contracts import schemas as s
from packages.domain import cp_sat
from packages.domain.solver_progress import precheck_issues
from packages.domain.validation import evaluate
from tests.test_autoschedule_live_worker import generation_job


def reference(number):
    return f'00000000-0000-0000-0000-{number:012d}'


def test_precheck_grouping_counts_all_findings_and_bounds_only_samples_and_groups():
    findings = [{'code': 'TIME_OVERLAP', 'shift_id': reference(i), 'person_id': reference(100),
                 'message': 'Private name and reason', 'detail': {'private': 'reason'}} for i in range(5)]
    findings.append(findings[0])
    issues = precheck_issues(findings, 'protected')
    assert issues == [{'classification': 'protected', 'code': 'TIME_OVERLAP', 'count': 6,
        'samples': [{'shift_id': reference(i), 'person_id': reference(100)} for i in range(3)]}]
    assert precheck_issues([{'code': 'UNSUPPORTED_RULE', 'rule_id': reference(1)}], 'blocking')[0]['samples'] == [
        {'rule_id': reference(1)}]
    groups = precheck_issues([{'code': f'CODE_{i:03}'} for i in range(60)], 'unresolved')
    assert len(groups) == s.AUTOSCHEDULE_PRECHECK_LIMITS['issues']
    assert groups[0] == {'classification': 'unresolved', 'code': 'CODE_000', 'count': 1, 'samples': []}


def test_precheck_groups_coalesce_later_checks_without_mutating_prior_updates():
    previous = precheck_issues([{'code': 'COVERAGE_GAP', 'shift_id': reference(1)}], 'protected')
    stored = copy.deepcopy(previous)
    issues = precheck_issues([{'code': 'COVERAGE_GAP', 'shift_id': reference(i)} for i in range(2, 6)],
                            'protected', previous=previous)
    assert issues == [{'classification': 'protected', 'code': 'COVERAGE_GAP', 'count': 5,
        'samples': [{'shift_id': reference(i)} for i in range(1, 4)]}]
    assert previous == stored
    distinct = precheck_issues([{'code': 'COVERAGE_GAP'}], 'outside_scope', previous=issues)
    assert len(distinct) == 2 and sum(issue['count'] for issue in distinct) == 6


@pytest.mark.parametrize('invalid', [
    {'count': 0}, {'count': True}, {'classification': 'infeasible'},
    {'samples': [{'shift_id': reference(1)}] * 4},
])
def test_progress_contract_rejects_invalid_precheck_evidence(invalid):
    issue = {'classification': 'protected', 'code': 'TIME_OVERLAP', 'count': 1, 'samples': []}
    with pytest.raises(ValidationError):
        s.AutoscheduleProgress.model_validate({'stage': 'checking', 'precheck_issues': [{**issue, **invalid}]})
    with pytest.raises(ValidationError):
        s.AutoscheduleProgress.model_validate({'stage': 'checking', 'precheck_issues': [issue] * 51})


def test_progress_stage_labels_preserve_existing_and_extensible_stage_values():
    for stage in [*s.GENERATION_PROGRESS_STAGES, 'engine_specific_stage']:
        assert s.AutoscheduleProgress(stage=stage).stage == stage
    assert [s.GENERATION_PROGRESS_STAGES[stage] for stage in ('checking', 'preparing', 'finding', 'scoring')] == [
        'Checking rules', 'Preparing', 'Searching', 'Scoring']
    assert 'scoring' not in s.GENERATION_SEARCH_FINISHED_STAGES


def test_precheck_references_are_detail_only_and_projected_without_mutating_storage():
    own, foreign = reference(1), reference(2)
    progress = {'stage': 'checking', 'precheck_issues': precheck_issues([
        {'code': 'TIME_OVERLAP', 'shift_id': sid, 'person_id': reference(100)} for sid in (own, foreign)], 'protected')}
    stored = copy.deepcopy(progress)
    assert job_progress_record(progress) == {'stage': 'checking'}
    detail = job_progress_record(progress, include_events=True, focus_ids=[own])
    assert detail['precheck_issues'][0]['count'] == 2
    assert detail['precheck_issues'][0]['samples'] == [{'shift_id': own, 'person_id': reference(100)}]
    assert job_progress_record(progress, include_events=True)['precheck_issues'][0]['samples'] == []
    assert progress == stored


def test_worker_retains_prechecks_between_stages_and_wakes_same_stage_readers(h, monkeypatch):
    workspace, job = generation_job(h)
    engine = h.app.state.engine
    issues = precheck_issues([{'code': 'TIME_OVERLAP'}], 'protected')
    claim = retry_write(engine, worker.claim_job)
    # 60315a5de made running-job progress private; the web client subscribes by job id.
    topic = worker.live.job_topic(h.seed['department_id'], job['id'])
    signals = []
    monkeypatch.setattr(worker.live, 'signal', lambda db, target, **change: signals.append(target))
    assert retry_write(engine, lambda db: worker.heartbeat_job(db, claim,
        {'stage': 'checking', 'precheck_issues': issues}))
    assert signals == [topic]
    signals.clear()
    assert retry_write(engine, lambda db: worker.heartbeat_job(db, claim,
        {'stage': 'checking', 'precheck_issues': copy.deepcopy(issues)}))
    assert not signals
    with transaction(engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.heartbeat_at = m.now() - timedelta(seconds=worker.JOB_LEASE_SECONDS + 1)
    final = []
    finish_job = worker.finish_job

    def finish(db, claim, result, error, *, progress=None, error_detail=None):
        final.append(copy.deepcopy(progress))
        return finish_job(db, claim, result, error, progress=progress, error_detail=error_detail)

    def solve(snapshot, *, progress, cancelled):
        progress({'stage': 'checking', 'precheck_issues': issues})
        progress({'stage': 'preparing'})
        progress({'stage': 'finding'})
        progress({'stage': 'scoring'})
        progress({'stage': 'ready'})
        return {'status': 'candidate_feasible', 'additions': [], 'removals': []}

    monkeypatch.setattr(worker, 'finish_job', finish)
    assert worker.process_one(engine, scheduler=solve)
    assert final[0]['precheck_issues'] == issues
    assert final[0]['stage'] == 'ready'
    assert 'precheck_issues' not in h.get('coordinator', '/api/jobs/' + job['id'])['job']['progress']


def test_protected_findings_arrive_while_checking_before_candidate_preparation(h, monkeypatch):
    workspace = h.shift(h.workspace())['workspace']
    workspace = h.shift(workspace, start='2026-10-04T09:00:00+03:00',
                        end='2026-10-04T13:00:00+03:00')['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    snapshot = h.snapshot(workspace)
    for shift in snapshot['shifts']:
        shift['locked'] = bool(shift['participants'])
    updates, prepared = [], []
    candidate_evidence = cp_sat.candidate_evidence

    def prepare(*args, **kwargs):
        assert updates[0]['stage'] == 'checking'
        early = [update for update in updates if any(issue['code'] == 'TIME_OVERLAP'
            and issue['classification'] == 'protected' for issue in update.get('precheck_issues', []))]
        assert early and early[0]['stage'] == 'checking'
        assert all('message' not in issue for update in early for issue in update['precheck_issues'])
        prepared.append(True)
        return candidate_evidence(*args, **kwargs)

    monkeypatch.setattr(cp_sat, 'candidate_evidence', prepare)
    result = cp_sat.generate(snapshot, 5, workers=1, progress=updates.append)
    assert prepared
    assert result['status'] == 'candidate_with_gaps', result
    final = next(finding for finding in result['validation']['findings'] if finding['code'] == 'TIME_OVERLAP')
    expected = next(finding for finding in evaluate(snapshot)['findings'] if finding['code'] == 'TIME_OVERLAP')
    assert final == expected
