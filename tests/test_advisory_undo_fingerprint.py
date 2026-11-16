"""Advisory review metadata keeps scheduling receipts current; real edits still invalidate them."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.issues import issue_key
from apps.worker.main import process_one
from tests.test_autoschedule_backend_audit import run_job
from tests.test_contextual_feasibility import activate
from tests.test_resolve_issues import resolve


def advisory_rota(h, *, staffed=False):
    person = h.seed['people'][0]
    h.policy({'kind': 'restriction'}, {'person_id': person}, severity='advisory')
    workspace = h.shift(h.workspace(), people=[person])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[person] if staffed else [])['workspace']
    keys = [issue_key(f) for f in workspace['validation']['findings'] if f['severity'] == 'warning']
    assert len(keys) == (2 if staffed else 1)
    return workspace, keys


def test_pending_proposal_stays_current_through_accept_and_undo_and_can_apply(h):
    workspace, keys = advisory_rota(h)
    before = h.detail(workspace)
    job = run_job(h, workspace)
    assert job['result']['additions'] and not job['result_stale']
    accepted = resolve(h, workspace, 'accept', keys)['workspace']
    assert accepted['revision'] == workspace['revision'] + 1
    assert accepted['content_revision'] == workspace['content_revision']
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['result_stale'] is False
    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Reconsider advisory acceptance'},
                       accepted['id'], accepted['revision'])['workspace']
    after = h.detail(undone)
    assert not undone['accepted_issues']
    assert undone['revision'] == accepted['revision'] + 1
    assert undone['content_revision'] == workspace['content_revision']
    assert after['current_fingerprint'] == before['current_fingerprint']
    assert after['shifts'] == before['shifts']
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['result_stale'] is False
    assert resolve(h, workspace, 'accept', keys, status=409)['error']['code'] == 'STALE_REVISION'
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.AuditEvent.action).where(
            m.AuditEvent.action.in_(['issues.resolve', 'workspace.undo'])).order_by(m.AuditEvent.created_at))) == [
                'issues.resolve', 'workspace.undo']
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
        'job_id': job['id'], 'reason': 'Apply the still-current proposal'}, revision=undone['revision'])
    assert applied['workspace']['content_revision'] == workspace['content_revision'] + 1


def test_real_assignment_change_and_its_undo_keep_a_pending_proposal_stale(h):
    workspace, _ = advisory_rota(h)
    before = h.detail(workspace)
    job = run_job(h, workspace)
    shift = before['shifts'][0]
    changed = h.command('coordinator', 'shift.update', {
        **{key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')},
        'workspace_id': workspace['id'], 'shift_id': shift['id'],
        'participants': [{'person_id': h.seed['people'][1], 'start': shift['start'], 'end': shift['end']}]},
        revision=workspace['revision'])['workspace']
    assert changed['content_revision'] == workspace['content_revision'] + 1
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['result_stale'] is True
    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Restore the original assignment'},
                       changed['id'], changed['revision'])['workspace']
    assert undone['content_revision'] == changed['content_revision'] + 1
    after = h.detail(undone)
    assert after['shifts'][0]['participants'][0]['person_id'] == h.seed['people'][0]
    assert after['current_fingerprint'] != before['current_fingerprint']
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['result_stale'] is True
    retained = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
        'job_id': job['id'], 'reason': 'Reconcile retained proposal'}, revision=undone['revision'])
    assert retained['reconciliation']['status'] in {'applied', 'content_conflict', 'unresolved'}
    assert retained['reconciliation']['source_fingerprint'] != retained['reconciliation']['current_fingerprint']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_immediate_assignment_save_then_undo_is_a_draft_edit_not_a_review_conflict(h):
    workspace, _ = advisory_rota(h)
    before = h.detail(workspace)
    shift = before['shifts'][0]
    saved = h.command('coordinator', 'shift.update', {
        **{key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')},
        'workspace_id': workspace['id'], 'shift_id': shift['id'],
        'participants': [{'person_id': h.seed['people'][1], 'start': shift['start'], 'end': shift['end']}]},
        revision=workspace['revision'])['workspace']
    assert saved['status'] == 'draft'
    checked = h.command('coordinator', 'workspace.validate', {'reason': 'Refresh derived findings'},
                        saved['id'], saved['revision'])['workspace']
    assert checked['revision'] == saved['revision']
    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Undo the replacement immediately'},
                       saved['id'], saved['revision'])['workspace']
    assert undone['status'] == 'draft'
    restored = h.detail(undone)
    assert restored['shifts'][0]['participants'][0]['person_id'] == h.seed['people'][0]
    assert undone['submitted_fingerprint'] is None


@pytest.mark.parametrize('repeat_same_key', [False, True], ids=['consecutive-accepts', 'repeat-accept'])
@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_metadata_undo_selects_latest_edit_even_when_timestamps_tie_and_never_reuses_an_undo(request, monkeypatch, repeat_same_key, database):
    h = request.getfixturevalue(database)
    workspace, keys = advisory_rota(h, staffed=True)
    before = h.detail(workspace)
    instant = m.now()
    monkeypatch.setattr(m, 'clock_now', lambda: instant)
    first = resolve(h, workspace, 'accept', keys[:1])['workspace']
    second_keys = keys[:1] if repeat_same_key else keys[1:]
    second = resolve(h, first, 'accept', second_keys)['workspace']
    assert second['content_revision'] == workspace['content_revision']
    assert len(second['accepted_issues']) == (1 if repeat_same_key else 2)
    for attempt in range(2):
        current = second if attempt == 0 else resolve(h, undone, 'accept', second_keys)['workspace']
        undone = h.command('coordinator', 'workspace.undo', {'reason': 'Undo only the latest acceptance'},
                           current['id'], current['revision'])['workspace']
        assert undone['accepted_issues'] == first['accepted_issues']
        assert undone['content_revision'] == workspace['content_revision']
        refused = h.command('coordinator', 'workspace.undo', {'reason': 'An undo is never redone'},
                            undone['id'], undone['revision'], status=409)
        assert refused['error']['code'] == 'NO_UNDO'
        assert h.detail(undone)['current_fingerprint'] == before['current_fingerprint']
        assert h.detail(undone)['shifts'] == before['shifts']


def test_advisory_undo_does_not_unapply_an_applied_proposal(h):
    workspace, keys = advisory_rota(h)
    job = run_job(h, workspace)
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
        'job_id': job['id'], 'reason': 'Apply scheduling suggestions'}, revision=workspace['revision'])['workspace']
    accepted = resolve(h, applied, 'accept', keys)['workspace']
    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Undo review metadata only'},
                       accepted['id'], accepted['revision'])['workspace']
    assert undone['content_revision'] == applied['content_revision']
    reread = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert reread['status'] == 'applied'
    assert reread['result']['applied_content_revision'] == applied['content_revision']


def test_pending_rule_approval_receipt_remains_current_after_advisory_undo(h):
    workspace, keys = advisory_rota(h, staffed=True)
    policy = h.command('coordinator', 'policy.create', {
        'name': 'Pending weekly maximum', 'owner': 'Synthetic reviewer', 'source': 'Constructed rule approval',
        'starts_on': '2020-01-01', 'definition': {'rule': {'kind': 'max_duties', 'maximum': 5, 'window': 'week'}}})['record']
    job = h.command('approver', 'job.create', {'workspace_id': workspace['id'], 'kind': 'feasibility',
        'check_seconds': 10, 'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]},
        revision=workspace['revision'])['job']
    assert process_one(h.app.state.engine, solver_workers=1)
    assert h.get('approver', '/api/jobs/' + job['id'])['job']['check_stale'] is False
    accepted = resolve(h, workspace, 'accept', keys)['workspace']
    h.command('coordinator', 'workspace.undo', {'reason': 'Reconsider advisory while rule approval waits'},
              accepted['id'], accepted['revision'])
    assert h.get('approver', '/api/jobs/' + job['id'])['job']['check_stale'] is False
    assert activate(h, policy, job['id'])['records'][0]['status'] == 'active'


def test_submitted_review_rejects_accept_and_undo_without_invalidating_approval(h):
    workspace, keys = advisory_rota(h, staffed=True)
    submitted = h.lifecycle(workspace, 'submit')['workspace']
    before = h.detail(submitted)
    assert resolve(h, submitted, 'accept', keys, status=409)['error']['code'] == 'WORKFLOW_STATE'
    assert h.command('coordinator', 'workspace.undo', {'reason': 'Submitted candidates cannot be edited'},
                     submitted['id'], submitted['revision'], status=409)['error']['code'] == 'WORKFLOW_STATE'
    assert h.detail(submitted)['workspace'] == before['workspace']
    approved = h.lifecycle(submitted, 'approve', 'approver')['workspace']
    assert approved['status'] == 'approved'
    assert approved['submitted_fingerprint'] == before['current_fingerprint']
