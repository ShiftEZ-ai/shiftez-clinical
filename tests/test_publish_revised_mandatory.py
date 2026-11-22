"""Review gates bind a mandatory cap revised to a lower number after the rota was captured."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.snapshots import currency_hint, fingerprint
from packages.domain.time import aware
from tests.clock_support import freeze_clock
from tests.test_overtime_extra_cover import extra_type, place_extra_cover
from tests.test_publish_mandatory import quota_settings, review_evidence
from tests.test_rule_catalogue_api import approve, line, person as person_scope, propose


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)], ids=['sqlite', 'postgresql'])
def captured(request, monkeypatch):
    freeze_clock(monkeypatch, aware('2026-10-04T00:00:00+03:00'))
    h = request.getfixturevalue(request.param)
    duty_type = extra_type(h)
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_overtime'}, {'person_id': person}, severity='advisory')
    draft = h.workspace()
    cap = h.policy({'kind': 'block_quota', **quota_settings(draft), 'comparison': 'maximum', 'amount': 5},
                   {'person_id': person}, severity='mandatory')
    # Recapture so the cap of 5 is part of the frozen rules.
    draft = h.workspace()
    for day in ('2026-10-04', '2026-10-05'):
        draft = place_extra_cover(h, draft, duty_type, person, day)['workspace']
    assert cap['id'] in {p['id'] for p in h.snapshot(draft)['policies']}
    return h, draft, cap


def revise(h, cap, amount=1, *, field='amount'):
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, cap['id'])
        row.definition = {**row.definition, 'rule': {**row.definition['rule'], field: amount}}
        row.revision += 1
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1


def assert_revised_refusal(error, cap, person):
    assert error['code'] == 'VALIDATION_FAILED', error
    finding, = [row for row in error['details']['findings'] if row['rule_id'] == cap['id']]
    assert finding['code'] == 'QUOTA_ABOVE_MAXIMUM' and finding['person_id'] == person
    assert finding['severity'] == 'error' and finding['detail']['actual'] == 2 and finding['detail']['amount'] == 1


def test_submit_refuses_a_mandatory_cap_revised_after_capture(captured):
    h, draft, cap = captured
    revise(h, cap)
    stored = review_evidence(h, draft)
    error = h.lifecycle(draft, 'submit', status=422)['error']
    assert_revised_refusal(error, cap, h.seed['people'][0])
    assert error['details']['refresh_baseline'] is True
    assert review_evidence(h, draft) == stored
    check = h.lifecycle(draft, 'validate')['validation']
    assert [f['code'] for f in check['findings'] if f.get('rule_id') == cap['id']] == ['QUOTA_ABOVE_MAXIMUM']


def test_approve_and_publish_refuse_a_cap_revised_after_submit(captured):
    h, draft, cap = captured
    submitted = h.lifecycle(draft, 'submit')['workspace']
    revise(h, cap)
    stale = h.lifecycle(submitted, 'approve', 'approver', status=409)
    assert stale['error']['code'] == 'STALE_INPUTS', stale
    reopened = h.lifecycle(submitted, 'reopen')['workspace']
    assert_revised_refusal(h.lifecycle(reopened, 'submit', status=422)['error'], cap, h.seed['people'][0])
    assert h.get('coordinator', '/api/publications')['publications'] == []


def test_publish_refuses_a_cap_revised_after_approve(captured):
    h, draft, cap = captured
    submitted = h.lifecycle(draft, 'submit')['workspace']
    approved = h.lifecycle(submitted, 'approve', 'approver')['workspace']
    revise(h, cap)
    stored = review_evidence(h, approved)
    refused = h.lifecycle(approved, 'publish', status=422)['error']
    assert_revised_refusal(refused, cap, h.seed['people'][0])
    assert review_evidence(h, approved) == stored
    assert h.get('coordinator', '/api/publications')['publications'] == []


def test_a_cap_revised_to_advisory_does_not_refuse_review(captured):
    h, draft, cap = captured
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, cap['id'])
        row.definition = {**row.definition, 'severity': 'advisory', 'rule': {**row.definition['rule'], 'amount': 1}}
        row.revision += 1
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1

    check = h.lifecycle(draft, 'validate')['validation']
    assert not any(f['rule_id'] == cap['id'] and f['severity'] == 'error' for f in check['findings'])
    submitted = h.lifecycle(draft, 'submit')['workspace']
    approved = h.lifecycle(submitted, 'approve', 'approver')['workspace']
    assert h.lifecycle(approved, 'publish')['publication_id']


@pytest.mark.parametrize('change', ['revision', 'approved_person_line'])
def test_relaxed_mandatory_cap_supplies_evidence_through_publication(h, monkeypatch, change):
    freeze_clock(monkeypatch, aware('2026-10-04T00:00:00+03:00'))
    duty_type = extra_type(h)
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_overtime'}, {'person_id': person}, severity='advisory')
    settings = quota_settings(h.workspace())
    if change == 'revision':
        captured_cap = h.policy({'kind': 'block_quota', **settings, 'comparison': 'maximum', 'amount': 1},
                                {'person_id': person})
        draft = h.workspace()
        revise(h, captured_cap, 3)
        cap = captured_cap
    else:
        from tests.test_rule_catalogue_v2_api import proposed as propose_conversion

        conversion = propose_conversion(h)
        h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'},
                  conversion['id'], conversion['revision'])
        settings = {'block_quota': settings}
        everyone = line(settings=settings, numbers={'max': {'value': 0, 'level': 'mandatory'}})
        captured_cap = approve(h, propose(h, 'workload_limit', [everyone], catalogue_version=2)['record'])
        draft = h.workspace()
        specific = line(settings=settings, who=person_scope(person), numbers={'max': {'value': 3, 'level': 'mandatory'}})
        cap = approve(h, propose(h, 'workload_limit', [everyone, specific], catalogue_version=2,
                                 policy_id=captured_cap['id'], revision=captured_cap['revision'],
                                 acknowledged=[everyone['id']])['record'])
    for day in ('2026-10-04', '2026-10-05'):
        draft = place_extra_cover(h, draft, duty_type, person, day)['workspace']

    def assert_current_check(workspace, expected_fingerprint):
        _, check, *_ = review_evidence(h, workspace)
        row, = [row for row in check['quota_report']['rows'] if row['person_id'] == person]
        assert (row['policy_id'], row['actual'], row['amount']) == (cap['id'], 2, 3)
        assert check['fingerprint'] == check['input_manifest']['token'] == expected_fingerprint
        if workspace['status'] in {'submitted', 'approved'}:
            with transaction(h.app.state.engine) as db:
                stored = db.get(m.Workspace, workspace['id'])
                assert fingerprint(db, stored) == expected_fingerprint
                hint = currency_hint(db, stored)
                assert check['input_manifest']['dirty_hint']['draft_policy_basis'] == hint['draft_policy_basis']

    submitted = h.lifecycle(draft, 'submit')['workspace']
    submitted_fp = submitted['submitted_fingerprint']
    assert_current_check(submitted, submitted_fp)
    approved = h.lifecycle(submitted, 'approve', 'approver')['workspace']
    assert_current_check(approved, submitted_fp)
    published = h.lifecycle(approved, 'publish')
    assert published['publication_id']
    assert_current_check({**approved, 'status': 'published'}, submitted_fp)


def test_loosened_safety_revision_keeps_the_captured_minimum(h, monkeypatch):
    freeze_clock(monkeypatch, aware('2026-10-04T00:00:00+03:00'))
    minimum = h.policy({'kind': 'minimum_cover', 'minimum': 2})
    draft = h.shift(h.workspace())['workspace']
    revise(h, minimum, 1, field='minimum')
    error = h.lifecycle(draft, 'submit', status=422)['error']
    assert error['code'] == 'VALIDATION_FAILED'
    finding, = [finding for finding in error['details']['findings'] if finding['rule_id'] == minimum['id']]
    assert finding['code'] == 'SCOPED_COVERAGE_GAP'
    assert 'department' in error['message'].lower() and 'Refresh' in error['message']
    assert error['details']['refresh_baseline'] is True

    refreshed = h.refresh_draft_baseline(draft)
    assert h.publish(refreshed)['publication_id']
