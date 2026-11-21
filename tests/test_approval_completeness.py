"""A saved-assignment delta never substitutes for a complete approval comparison."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.snapshots import currency_hint, currency_match, feasibility_fingerprints
from apps.worker.main import process_one
from packages.domain import feasibility
from .test_contextual_feasibility import use_checked_activation
from .test_rule_catalogue_api import line, propose


@pytest.fixture(params=['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)])
def backend(request):
    return request.getfixturevalue('pg' if request.param == 'postgresql' else 'h')


@pytest.fixture
def approval_receipt(backend):
    return checked_approval(backend)


def checked_approval(h, **fields):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    policy = propose(h, 'duties_per_week', [line(numbers={'max': {'value': 5}}, **fields)])['record']
    use_checked_activation(h)
    h.clients.clear()
    job = h.command('approver', 'job.create', {'workspace_id': workspace['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]},
        revision=workspace['revision'])['job']
    assert process_one(h.app.state.engine)
    return h, workspace, policy, job


@pytest.mark.parametrize('change', ['none', 'other_workspace', 'other_rota_shift'])
def test_review_ready_receipt_remains_activatable_after_unrelated_changes(approval_receipt, change):
    h, workspace, policy, job = approval_receipt
    if change != 'none':
        other = h.workspace('Unrelated historical rota', start='2025-01-05', end='2025-01-12')
        if change == 'other_rota_shift':
            h.shift(other, start='2025-01-06T08:00:00+03:00', end='2025-01-06T12:00:00+03:00', people=[])
    with transaction(h.app.state.engine) as db:
        current = db.get(m.Workspace, workspace['id'])
        receipt = db.get(m.Job, job['id'])
        hint = receipt.read_metadata['input_snapshot']['currency_hint']
        # Unrelated changes leave the fingerprint unchanged, so the hint stays current (17 Nov oracle).
        assert currency_match(hint, currency_hint(db, current)) is True
        assert receipt.input_fingerprint == feasibility_fingerprints(db, [current])[current.id]
    review = h.get('approver', f"/api/policies/{policy['id']}/review")
    effect, = review['effect']
    assert review['ready'] and effect['status'] == 'ready' and effect['job_id'] == job['id']
    activated = h.command('approver', 'policy.activate', {'reason': 'Independent current receipt review',
        'check_job_ids': [job['id']]}, policy['id'], policy['revision'])
    assert activated['record']['status'] == 'active'
    assert activated['context_check_evidence'][0]['check_job_id'] == job['id']


def test_reviewed_rota_edit_requires_a_fresh_receipt_before_activation(approval_receipt):
    h, workspace, policy, job = approval_receipt
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    review = h.get('approver', f"/api/policies/{policy['id']}/review")
    effect, = review['effect']
    assert not review['ready'] and effect['status'] == 'not_checked'
    refused = h.command('approver', 'policy.activate', {'reason': 'Stale receipt', 'check_job_ids': [job['id']]},
        policy['id'], policy['revision'], status=409)
    assert refused['error']['code'] == 'RULE_CHECK_REQUIRED'
    fresh = h.command('approver', 'job.create', {'workspace_id': workspace['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]},
        revision=workspace['revision'])['job']
    assert fresh['id'] != job['id'] and process_one(h.app.state.engine)
    review = h.get('approver', f"/api/policies/{policy['id']}/review")
    effect, = review['effect']
    assert review['ready'] and effect['status'] == 'ready' and effect['job_id'] == fresh['id']
    activated = h.command('approver', 'policy.activate', {'reason': 'Independent fresh receipt review',
        'check_job_ids': [fresh['id']]}, policy['id'], policy['revision'])
    assert activated['record']['status'] == 'active'
    assert activated['context_check_evidence'][0]['check_job_id'] == fresh['id']


def test_temporary_binding_is_rejected_by_review_and_activation(approval_receipt):
    h, _, policy, job = approval_receipt
    with transaction(h.app.state.engine, write=True) as db:
        receipt = db.get(m.Job, job['id'])
        result = deepcopy(receipt.result)
        # Keep the hashes current so this isolates the temporary-change guard in both consumers.
        result['bindings'][0]['temporary_change'] = True
        receipt.result = result
    review = h.get('approver', f"/api/policies/{policy['id']}/review")
    effect, = review['effect']
    assert not review['ready'] and effect['status'] == 'not_checked'
    refused = h.command('approver', 'policy.activate', {'reason': 'Temporary changes cannot approve the saved rule',
        'check_job_ids': [job['id']]}, policy['id'], policy['revision'], status=409)
    assert refused['error']['code'] == 'RULE_CHECK_REQUIRED'


@pytest.mark.parametrize('safety', [False, True])
@pytest.mark.parametrize('complete', [False, True])
def test_review_and_activation_share_completeness_and_safety(backend, monkeypatch, safety, complete):
    h = backend
    workspace = h.shift(h.workspace(), people=[])['workspace']
    policy = propose(h, 'people_per_session' if safety else 'duties_per_week',
        [line(numbers={'max': {'value': 5}})])['record']
    use_checked_activation(h)
    h.clients.clear()
    generate = feasibility.generate
    def solve(*args, **kwargs):
        result = generate(*args, **kwargs)
        assert result['status'] == 'candidate_feasible'
        if not complete:
            result['status'] = 'unknown_time_limit'
        return result
    monkeypatch.setattr(feasibility, 'generate', solve)
    job = h.command('approver', 'job.create', {'workspace_id': workspace['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]},
        revision=workspace['revision'])['job']
    assert process_one(h.app.state.engine)
    checked = h.get('approver', f"/api/jobs/{job['id']}")['job']['result']
    assert checked['effect']['status'] == 'ready'
    assert checked['comparison_complete'] is complete
    review = h.get('approver', f"/api/policies/{policy['id']}/review")
    effect, = review['effect']
    assert effect['status'] == ('ready' if complete else 'unresolved')
    assert effect['comparison_complete'] is complete
    assert review['ready'] is complete
    assert review['requires_acknowledgement'] is (not complete)
    assert review['can_decide'] is (complete or not safety)
    if not complete:
        assert 'Time limit reached' in effect['summary'] and 'No change' not in effect['summary']
    payload = {'reason': 'Independently reviewed the incomplete search', 'check_job_ids': [job['id']]}
    if not complete:
        denied = h.command('approver', 'policy.activate', payload, policy['id'], policy['revision'], status=409)
        assert denied['error']['code'] == 'RULE_CHECK_UNRESOLVED'
        no_reason = h.command('approver', 'policy.activate', {**payload, 'reason': '', 'acknowledge_unresolved': True},
            policy['id'], policy['revision'], status=(409, 422))
        assert no_reason.get('error')
        payload['acknowledge_unresolved'] = True
    decision = h.command('approver', 'policy.activate', payload, policy['id'], policy['revision'],
        status=409 if safety and not complete else 200)
    if safety and not complete:
        assert decision['error']['code'] == 'RULE_CHECK_UNRESOLVED'
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Policy, policy['id']).status == 'proposed'
        return
    assert decision['record']['status'] == 'active'
    evidence, = decision['context_check_evidence']
    assert evidence['comparison_complete'] is complete
    assert bool(evidence.get('acknowledged_unresolved')) is (not complete)
    if not complete:
        assert evidence['effect']['status'] == 'unresolved'
        assert 'Time limit reached' in evidence['summary']
    with transaction(h.app.state.engine) as db:
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'policy.activate',
            m.AuditEvent.target_id == policy['id']))
        assert audit.detail['context_check_evidence'] == decision['context_check_evidence']


@pytest.mark.parametrize('with_receipt', [False, True])
def test_safety_cannot_acknowledge_missing_or_incomplete_receipt(backend, with_receipt):
    h = backend
    workspace = h.shift(h.workspace(), people=[])['workspace']
    policy = propose(h, 'people_per_session', [line(numbers={'min': {'value': 1}})])['record']
    use_checked_activation(h)
    h.clients.clear()
    ids = []
    if with_receipt:
        job = h.command('approver', 'job.create', {'workspace_id': workspace['id'], 'kind': 'feasibility',
            'rule_changes': [{'policy_id': policy['id'], 'expected_revision': policy['revision']}]},
            revision=workspace['revision'])['job']
        assert process_one(h.app.state.engine)
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Job, job['id'])
            row.result = {**deepcopy(row.result), 'comparison_complete': False}
        ids = [job['id']]
    review = h.get('approver', f"/api/policies/{policy['id']}/review")
    assert not review['can_decide']
    denied = h.command('approver', 'policy.activate', {'reason': 'Safety cannot accept uncertainty',
        'acknowledge_unresolved': True, 'check_job_ids': ids}, policy['id'], policy['revision'], status=409)
    assert denied['error']['code'] == 'RULE_CHECK_UNRESOLVED'
