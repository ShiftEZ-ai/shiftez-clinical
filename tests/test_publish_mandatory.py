"""Review gates check mandatory rules activated after the rota was captured."""
import pytest
from sqlalchemy import select

from apps.api import models as m, workspace_baselines
from apps.api.db import transaction
from apps.api.serialization import record
from packages.domain.time import aware
from tests.clock_support import freeze_clock
from tests.test_overtime_extra_cover import extra_type, place_extra_cover


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)], ids=['sqlite', 'postgresql'])
def rota(request, monkeypatch):
    freeze_clock(monkeypatch, aware('2026-10-04T00:00:00+03:00'))
    h = request.getfixturevalue(request.param)
    duty_type = extra_type(h)
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_overtime'}, {'person_id': person}, severity='advisory')
    draft = h.workspace()
    for day in ('2026-10-04', '2026-10-05'):
        draft = place_extra_cover(h, draft, duty_type, person, day)['workspace']
    return h, draft


def quota_settings(draft):
    return {'metric': 'duties', 'overtime': 'only',
            'starts_on': draft['starts_on'], 'ends_on': draft['ends_on']}


def activate_cap(h, draft, *, amount=0, severity='mandatory'):
    return h.policy({'kind': 'block_quota', **quota_settings(draft), 'comparison': 'maximum', 'amount': amount},
                    {'person_id': h.seed['people'][0]}, severity=severity)


def assert_cap_refusal(error, policy, person):
    assert error['code'] == 'VALIDATION_FAILED'
    finding, = [row for row in error['details']['findings'] if row['rule_id'] == policy['id']]
    assert finding['code'] == 'QUOTA_ABOVE_MAXIMUM'
    assert finding['person_id'] == person and finding['severity'] == 'error'
    assert finding['detail']['actual'] == 2 and finding['detail']['amount'] == policy['definition']['rule']['amount']


def review_evidence(h, workspace):
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace['id'])
        return (row.status, row.validation, row.review_snapshot, row.submitted_fingerprint,
                *([record(item) for item in db.scalars(select(cls).where(cls.workspace_id == row.id))]
                  for cls in (m.Approval, m.ReviewDecision)))


def review_with_legacy_rules(h, workspace, monkeypatch, action='submit', role='coordinator'):
    overlay = workspace_baselines.live_safety_inputs

    def frozen_rules(*args, **kwargs):
        return overlay(*args, **{**kwargs, 'include_mandatory': False, 'current_mandatory': False})

    with monkeypatch.context() as legacy:
        legacy.setattr(workspace_baselines, 'live_safety_inputs', frozen_rules)
        return h.lifecycle(workspace, action, role)['workspace']


def test_submit_refuses_a_mandatory_cap_activated_after_capture(rota):
    h, draft = rota
    cap = activate_cap(h, draft, amount=1)
    stored = review_evidence(h, draft)
    refused = h.lifecycle(draft, 'submit', status=422)
    assert_cap_refusal(refused['error'], cap, h.seed['people'][0])
    assert review_evidence(h, draft) == stored


@pytest.mark.parametrize('activation', ['after_capture', 'after_submit', 'after_approve'])
def test_publish_refuses_a_late_mandatory_cap(rota, monkeypatch, activation):
    h, draft = rota
    # A zero is already live on the captured review basis.
    if activation == 'after_capture':
        cap = activate_cap(h, draft, amount=1)
        submitted = review_with_legacy_rules(h, draft, monkeypatch)
    else:
        submitted = h.lifecycle(draft, 'submit')['workspace']
    if activation == 'after_submit':
        cap = activate_cap(h, draft, amount=1)
        stale = h.lifecycle(submitted, 'approve', 'approver', status=409)
        assert stale['error']['code'] == 'STALE_INPUTS'
        # Earlier versions allowed the required resubmission without checking this cap.
        reopened = h.lifecycle(submitted, 'reopen')['workspace']
        submitted = review_with_legacy_rules(h, reopened, monkeypatch)
    # Earlier versions could also approve with a captured basis that missed this cap.
    approved = review_with_legacy_rules(h, submitted, monkeypatch, 'approve', 'approver')
    if activation == 'after_approve':
        cap = activate_cap(h, draft, amount=1)
    with transaction(h.app.state.engine) as db:
        baseline = workspace_baselines.verify(workspace_baselines.latest(db, approved['id']))
    assert cap['id'] not in {p['id'] for p in baseline['policies']}
    stored = review_evidence(h, approved)
    refused = h.lifecycle(approved, 'publish', status=422)['error']
    assert_cap_refusal(refused, cap, h.seed['people'][0])
    assert refused['details']['stale_approval']['code'] == 'STALE_APPROVAL'
    assert review_evidence(h, approved) == stored
    assert h.get('coordinator', '/api/publications')['publications'] == []


def test_publish_with_no_late_mandatory_rule(rota, monkeypatch):
    h, draft = rota
    submitted = h.lifecycle(draft, 'submit')['workspace']
    approved = h.lifecycle(submitted, 'approve', 'approver')['workspace']
    _, approved_check, *review = review_evidence(h, approved)
    published_at = freeze_clock(monkeypatch, aware('2026-10-04T00:01:00+03:00'))
    assert h.lifecycle(approved, 'publish')['publication_id']
    status, published_check, *recorded_review = review_evidence(h, approved)
    assert status == 'published' and recorded_review == review
    assert published_check['evaluated_at'] == published_at.isoformat() != approved_check['evaluated_at']
    assert published_check['fingerprint'] == approved_check['fingerprint']


def test_publish_with_an_unbreached_late_mandatory_cap(rota):
    h, draft = rota
    cap = activate_cap(h, draft, amount=2)
    current = h.lifecycle(draft, 'validate')['validation']
    assert current['valid']
    row, = [row for row in current['quota_report']['rows'] if row['policy_id'] == cap['id']]
    assert row['actual'] == row['amount'] == 2

    submitted = h.lifecycle(draft, 'submit')['workspace']
    approved = h.lifecycle(submitted, 'approve', 'approver')['workspace']
    assert h.lifecycle(approved, 'publish')['publication_id']
    _, stored, *_ = review_evidence(h, approved)
    assert stored['fingerprint'] == approved['submitted_fingerprint']
    # Publication retains the current limit and count that qualified this rota.
    recorded, = [row for row in stored['quota_report']['rows'] if row['policy_id'] == cap['id']]
    assert recorded['actual'] == recorded['amount'] == 2


def test_publish_with_a_late_cap_and_an_approved_specific_line(rota):
    from tests.test_rule_catalogue_v2_api import proposed as propose_conversion

    h, draft = rota
    conversion = propose_conversion(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'},
              conversion['id'], conversion['revision'])
    settings = {'block_quota': quota_settings(draft)}
    everyone = {'id': m.uid(), 'settings': settings, 'numbers': {'max': {'value': 0, 'level': 'mandatory'}}}
    person = {'id': m.uid(), 'settings': settings, 'numbers': {'max': {'value': 2, 'level': 'mandatory'}},
              'who': {'conditions': [{'field': 'person_ids', 'values': [h.seed['people'][0]]}]}}
    proposed = h.command('coordinator', 'rule.propose', {'catalogue_id': 'workload_limit', 'catalogue_version': 2,
        'lines': [everyone, person], 'acknowledged_overlaps': [everyone['id']],
        'reason': 'Synthetic overtime limits'})['record']
    rule = h.command('approver', 'policy.activate', {'reason': 'Independent review'},
                     proposed['id'], proposed['revision'])['record']
    draft_check = h.lifecycle(draft, 'validate')['validation']
    assert draft_check['valid']
    assert [row['amount'] for row in draft_check['quota_report']['rows']
            if row['policy_id'] == rule['id'] and row['person_id'] == h.seed['people'][0]] == [2]
    assert h.publish(draft)['publication_id']


def test_late_advisory_activation_only_makes_approval_stale(rota):
    h, draft = rota
    submitted = h.lifecycle(draft, 'submit')['workspace']
    approved = h.lifecycle(submitted, 'approve', 'approver')['workspace']
    cap = activate_cap(h, draft, severity='advisory')
    stored = review_evidence(h, approved)
    refused = h.lifecycle(approved, 'publish', status=409)['error']
    assert refused['code'] == 'STALE_APPROVAL'
    assert not [finding for finding in (refused.get('details') or {}).get('findings', [])
                if finding['rule_id'] == cap['id']]
    assert review_evidence(h, approved) == stored
    assert h.get('coordinator', '/api/publications')['publications'] == []
