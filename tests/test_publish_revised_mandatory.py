"""Review gates bind a mandatory cap revised to a lower number after the rota was captured."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.time import aware
from tests.clock_support import freeze_clock
from tests.test_overtime_extra_cover import extra_type, place_extra_cover
from tests.test_publish_mandatory import quota_settings, review_evidence


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


def revise(h, cap, amount=1):
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, cap['id'])
        row.definition = {**row.definition, 'rule': {**row.definition['rule'], 'amount': amount}}
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
    assert_revised_refusal(h.lifecycle(draft, 'submit', status=422)['error'], cap, h.seed['people'][0])
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
