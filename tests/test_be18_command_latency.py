"""Scoped command checks never authorize a whole rota; the worker stores canonical complete evidence."""
import copy

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning
from apps.worker import main as worker
from apps.worker.main import process_one
from packages.domain import proposal_reconciliation
from packages.domain.validation import evaluate
from packages.domain.validation_scope import assignment_scope, evaluate_assignments
from packages.contracts.schemas import ParticipantInput
from .clock_support import freeze_clock
from .test_scoped_job_inputs import proposal, apply, h_assignments
from .test_writer_preparation import writer_evaluations  # noqa: F401


def worker_up(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.WorkerHeartbeat(worker_id='be18-test-worker', started_at=m.now(), last_seen_at=m.now()))


def test_push_only_pass_does_not_defer_participant_check(h, monkeypatch, tmp_path):
    monkeypatch.setattr(worker, 'ALIVE_FILE', str(tmp_path / 'worker-alive'))
    monkeypatch.setattr(worker, '_beat', {})
    worker.process_push_one(h.app.state.engine, h.settings, record_worker_heartbeat=False)
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.WorkerHeartbeat.worker_id)) is None
        assert not planning.background_checks_available(db)
    w = h.refresh_draft_baseline(h.workspace())
    w = h.shift(w, typ=optional_type(h))['workspace']
    shift = h.detail(w)['shifts'][0]
    result = edit(h, w, shift, [])
    assert result['workspace']['validation'].get('scope') != 'command_changes'
    summary = h.get('coordinator', f'/api/workspaces/{w["id"]}/summary')
    assert summary['worker_liveness'] == 'never_seen'
    assert summary['worker_heartbeat_max_age_seconds'] == h.settings.worker_heartbeat_max_age_seconds
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Job.id).where(m.Job.workspace_id == w['id'], m.Job.kind == 'validation')) is None


def optional_type(h):
    return h.configure('assignment_types', {'name': 'Optional attendance',
        'starts_on': '2020-01-01', 'definition': {'requirements': []}})['record']['id']


def edit(h, workspace, shift, participants, **kwargs):
    payload = {key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=workspace['id'], shift_id=shift['id'], participants=[
        ParticipantInput.model_validate({key: p[key] for key in ParticipantInput.model_fields if key in p}).model_dump(mode='json')
        for p in participants])
    return h.command('coordinator', 'shift.update', payload, revision=workspace['revision'], **kwargs)


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_edit_queues_coalesced_check_and_worker_stores_exact_complete_result(h, monkeypatch):
    freeze_clock(monkeypatch)
    w = h.workspace()
    typ = optional_type(h)
    w = h.refresh_draft_baseline(w)
    w = h.shift(w, typ=typ)['workspace']
    shift = h.detail(w)['shifts'][0]
    worker_up(h)
    from apps.api.services import jobs
    queued, queue_validation = [], jobs.queue_validation
    def queue_once(db, *args):
        preparation = db.info.get('_planning_preparation')
        assert preparation is None or preparation['replay']
        queued.append(args[1].id)
        return queue_validation(db, *args)
    monkeypatch.setattr(jobs, 'queue_validation', queue_once)
    def no_empty_pairing(*args):
        pytest.fail('An empty assignment edit has no supervisor pairs to name')
    monkeypatch.setattr(planning, 'name_attached_supervisors', no_empty_pairing)
    result = edit(h, w, shift, [])
    assert queued == [w['id']]
    w = result['workspace']
    summary = h.get('coordinator', f'/api/workspaces/{w["id"]}/summary')
    assert summary['worker_liveness'] == 'running'
    assert summary['worker_heartbeat_max_age_seconds'] == h.settings.worker_heartbeat_max_age_seconds
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, w['id'])
        assert row.validation['scope'] == 'command_changes'
        assert row.validation['fingerprint'] != h.snapshot(w)['fingerprint']
        assert 'score' not in row.validation
        job = db.scalar(select(m.Job).where(m.Job.workspace_id == w['id'], m.Job.kind == 'validation'))
        job_id = job.id
        assert job.status == 'queued'
        expected = planning.revalidate(db, row, store=False, include_internal=True)
    same = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'},
                     revision=w['revision'])['job']
    assert same['id'] == job_id
    assert h.get('coordinator', f'/api/workspaces/{w["id"]}/summary')['fix_list']['summary']['checking']
    assert process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, w['id'])
        assert row.validation == expected
        assert not row.validation.get('scope')
        assert row.revision == w['revision']
        assert db.get(m.Job, job_id).status == 'succeeded'


def test_new_edit_supersedes_obsolete_check_and_old_claim_cannot_store(h):
    w = h.workspace()
    typ = optional_type(h)
    w = h.refresh_draft_baseline(w)
    w = h.shift(w, typ=typ)['workspace']
    shift = h.detail(w)['shifts'][0]
    original = copy.deepcopy(shift['participants'])
    worker_up(h)
    first = edit(h, w, shift, [])['workspace']
    from apps.worker.main import claim_job, finish_job
    with transaction(h.app.state.engine, write=True) as db:
        claim = claim_job(db)
    assert claim and claim['kind'] == 'validation'
    second = edit(h, first, shift, original)['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        assert not finish_job(db, claim, {'valid': True}, None)
        jobs = list(db.scalars(select(m.Job).where(m.Job.workspace_id == w['id'])))
        assert sorted(job.status for job in jobs) == ['failed', 'queued']
        assert db.get(m.Job, claim['id']).error_code == 'STALE_INPUTS'
        assert db.get(m.Workspace, w['id']).revision == second['revision']


def test_scoped_edit_retains_unrelated_overlap_evidence_and_clears_changed_peer_flags(h):
    typ = optional_type(h)
    first, other = h.seed['people'][:2]
    w = h.shift(h.workspace(), people=[first], typ=typ)['workspace']
    w = h.shift(w, people=[first], service=1, typ=typ)['workspace']
    for service in (0, 1):
        w = h.shift(w, people=[other], service=service, typ=typ, start='2026-10-05T08:00:00+03:00',
                    end='2026-10-05T12:00:00+03:00')['workspace']
    detail = h.detail(w)
    prior = [f for f in detail['workspace']['validation']['findings']
             if f['code'] == 'TIME_OVERLAP' and f['person_id'] == other]
    assert prior
    target = next(row for row in detail['shifts'] if row['participants'][0]['person_id'] == first)
    worker_up(h)
    result = edit(h, w, target, [])
    findings = result['validation']['findings']
    assert all(f in findings for f in prior)
    assert not any(f['code'] == 'TIME_OVERLAP' and f['person_id'] == first for f in findings)
    peers = [row for row in result['shifts'] if row['start'] == target['start']]
    assert len(peers) == 2 and all(not row['double_booking_conflicts'] for row in peers)


def test_manual_restriction_refusal_still_happens_before_queuing(h):
    pid = h.seed['people'][0]
    w = h.shift(h.workspace(), people=[])['workspace']
    shift = h.detail(w)['shifts'][0]
    h.policy({'kind': 'restriction'}, {'person_id': pid})
    worker_up(h)
    refused = edit(h, w, shift, [{'person_id': pid, 'start': shift['start'], 'end': shift['end']}], status=422)
    assert refused['error']['code'] == 'EDIT_INVALID'
    with transaction(h.app.state.engine) as db:
        assert not db.scalar(select(m.Job.id).where(m.Job.workspace_id == w['id'], m.Job.kind == 'validation'))
        assert db.get(m.Workspace, w['id']).revision == w['revision']


@pytest.mark.parametrize('rule', ['max_duties', 'min_rest', 'forbidden_transition', 'restriction'])
def test_apply_refuses_unsafe_bundles_synchronously_with_worker(h, rule):
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    pid = h.seed['people'][0]
    job, _, _ = proposal(h, w, [pid, pid])
    rules = {'max_duties': {'kind': 'max_duties', 'maximum': 1, 'window': 'week'},
             'min_rest': {'kind': 'min_rest', 'hours': 24},
             'forbidden_transition': {'kind': 'forbidden_transition', 'from_type_ids': [h.seed['type']],
                                      'to_type_ids': [h.seed['type']]},
             'restriction': {'kind': 'restriction'}}
    h.policy(rules[rule], {'person_id': pid})
    if rule == 'max_duties':
        # A non-safety limit stays frozen in the draft until its baseline is refreshed; the three safety kinds
        # must bind a draft live, without a refresh.
        w = h.refresh_draft_baseline(w)
    before = h.snapshot(w)
    worker_up(h)
    reply = apply(h, w, job)
    assigned = h_assignments(h, w)
    assert len(assigned) == 0
    assert reply['proposal_retained'] is True
    assert reply['reconciliation']['status'] == ('unresolved' if rule == 'restriction' else 'revalidation_required')
    assert reply['reconciliation']['addition_indexes'] == reply['reconciliation']['removal_indexes'] == []
    assert reply['reconciliation']['conflicting_placement_count'] >= 1
    assert proposal_reconciliation.comparison(evaluate(before), evaluate(h.snapshot(w)))['no_new_problems']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_fresh_review_gates_reuse_evidence_and_publish_reuses_it(h, monkeypatch, writer_evaluations):
    """Fresh submit and approve reuse evidence. Publish evaluates the complete rota under the writer."""
    w = h.shift(h.workspace())['workspace']
    def forbidden(*args, **kwargs):
        pytest.fail('A fresh submit or approve recalculated the rota')
    from packages.domain import score
    with monkeypatch.context() as patch:
        patch.setattr(planning, 'evaluate', forbidden)
        patch.setattr(score, 'schedule_score', forbidden)
        for action, role in [('submit', 'coordinator'), ('approve', 'approver')]:
            w = h.lifecycle(w, action, role)['workspace']
    writer_evaluations.clear()
    evaluate = planning.evaluate
    def fresh(data, **options):
        assert '_partial' not in data and not options.get('quota_only'), 'Publish must check the complete rota'
        return evaluate(data, **options)
    monkeypatch.setattr(planning, 'evaluate', fresh)
    published = h.lifecycle(w, 'publish')
    assert writer_evaluations == [True], 'Publish evaluates once under the writer and never during admission'
    assert published['workspace']['status'] == 'published'
    assert published['publication_id']


def test_publish_rechecks_when_approved_evidence_is_incomplete(h, monkeypatch):
    w = h.shift(h.workspace('Publish stale evidence'))['workspace']
    w = h.lifecycle(w, 'submit')['workspace']
    w = h.lifecycle(w, 'approve', 'approver')['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, w['id'])
        check = copy.deepcopy(row.validation)
        check.pop('score', None)
        row.validation = check
    calls, evaluate = [], planning.evaluate

    def counted(*args, **kwargs):
        calls.append(True)
        return evaluate(*args, **kwargs)

    monkeypatch.setattr(planning, 'evaluate', counted)
    published = h.lifecycle(w, 'publish')
    assert published['workspace']['status'] == 'published'
    assert calls == [True], 'Incomplete approval evidence must fall back to one fresh final check'


@pytest.mark.parametrize('invalid', ['partial', 'engine', 'contracts', 'coverage', 'score', 'input'])
@pytest.mark.parametrize('stage', ['submit', 'approve'])
@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_invalid_stored_evidence_forces_complete_review_check(h, monkeypatch, invalid, stage):
    w = h.shift(h.workspace())['workspace']
    if stage == 'approve':
        w = h.lifecycle(w, 'submit')['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, w['id'])
        check = copy.deepcopy(row.validation)
        if invalid == 'partial':
            check['scope'] = 'command_changes'
        elif invalid == 'engine':
            check['engine_version'] = 'old'
        elif invalid == 'contracts':
            check['contracts'] = {}
        elif invalid == 'coverage':
            check['coverage'] = []
        elif invalid == 'score':
            check.pop('score')
        else:
            db.get(m.Location, h.seed['locations'][0]).capacity += 1
        row.validation = check
    calls, original = [], planning.evaluate
    def counted(*args, **kwargs):
        calls.append(args[0].get('_partial'))
        return original(*args, **kwargs)
    monkeypatch.setattr(planning, 'evaluate', counted)
    result = h.lifecycle(w, stage, 'approver' if stage == 'approve' else 'coordinator',
                         status=409 if stage == 'approve' and invalid == 'input' else 200)
    if stage == 'approve' and invalid == 'input':
        assert result['error']['code'] == 'STALE_INPUTS'
    assert calls == [None]


def test_without_worker_assignment_edit_still_stores_complete_check(h):
    w = h.workspace()
    typ = optional_type(h)
    w = h.refresh_draft_baseline(w)
    w = h.shift(w, typ=typ)['workspace']
    result = edit(h, w, h.detail(w)['shifts'][0], [])
    with transaction(h.app.state.engine) as db:
        check = db.get(m.Workspace, w['id']).validation
        assert not check.get('scope') and 'score' in check and '_session_parts' in check
        assert not db.scalar(select(m.Job.id).where(m.Job.workspace_id == w['id']))
    assert result['validation']['fingerprint'] == h.snapshot(w)['fingerprint']


def test_scoped_comparison_matches_full_for_pooled_cover_and_person_windows(h):
    h.policy({'kind': 'minimum_cover', 'minimum': 1, 'aggregation': 'service_group'},
             {'service_ids': h.seed['services'][:2]})
    h.policy({'kind': 'min_rest', 'hours': 12})
    w = h.shift(h.workspace(), people=[h.seed['people'][0]])['workspace']
    w = h.shift(w, people=[], service=1)['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00',
                people=[h.seed['people'][0]])['workspace']
    before = h.snapshot(w)
    after = copy.deepcopy(before)
    changed = next(row for row in after['shifts'] if row['participants'])
    original = copy.deepcopy(changed)
    changed['participants'] = []
    scope = assignment_scope(after, [original], [changed])
    simultaneous = {row['id'] for row in before['shifts'] if row['start'] == changed['start']}
    assert simultaneous <= scope['shifts']
    full = proposal_reconciliation.comparison(evaluate(before), evaluate(after))
    partial = proposal_reconciliation.comparison(evaluate_assignments(before, scope), evaluate_assignments(after, scope))
    assert partial['no_new_problems'] == full['no_new_problems']
    assert partial['new_or_worse_findings'] == full['new_or_worse_findings']
