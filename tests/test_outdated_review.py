"""A rota submitted before rules or people changed is marked for resubmission, not offered for approval."""
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services.people import CONTACT_FIELDS
from packages.contracts.schemas import PersonInput


def test_submitted_rota_becomes_outdated_when_inputs_change(h):
    w = h.lifecycle(h.shift(h.workspace('Outdated review'))['workspace'], 'submit')['workspace']
    listed = lambda: next(x for x in h.get('approver', '/api/workspaces?limit=50&offset=0')['workspaces'] if x['id'] == w['id'])
    assert listed()['outdated'] is False
    h.policy({'kind': 'min_rest', 'hours': 11})
    assert listed()['outdated'] is True
    result = h.command('approver', 'workspace.approve', {'reason': 'Too late'}, w['id'], listed()['revision'], status=409)
    assert result['error']['code'] == 'STALE_INPUTS'
    assert 'submit it again before review' in result['error']['message']


def test_stale_approval_explains_recovery_and_keeps_recorded_evidence(h):
    w = h.lifecycle(h.shift(h.workspace('Recorded approval'))['workspace'], 'submit')['workspace']
    w = h.lifecycle(w, 'approve', 'approver')['workspace']

    def approvals():
        with transaction(h.app.state.engine) as db:
            return {model.__name__: {row.id: record(row) for row in db.scalars(
                select(model).where(model.workspace_id == w['id']))}
                for model in (m.Approval, m.ReviewDecision)}

    before = approvals()
    person = next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == h.seed['people'][-1])
    pager = next(row for row in h.get('admin', '/api/contact-types')['contact_types'] if row['legacy_field'] == 'pager')
    saved = h.command('admin', 'person.contact.set', {'person_id': person['id'], 'contact_type_id': pager['id'],
        'value': '4999', 'reason': 'Synthetic contact change'})['record']
    assert saved['value'] == '4999'
    assert h.detail(w)['current_fingerprint'] == w['submitted_fingerprint']
    person = next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == person['id'])
    h.configure('people', {**{k: person[k] for k in PersonInput.model_fields if k not in CONTACT_FIELDS}, 'active': False},
                person['id'], person['revision'])
    refused = h.lifecycle(w, 'publish', status=409)['error']
    assert refused['code'] == 'STALE_APPROVAL'
    assert 'Approval is still recorded' in refused['message']
    assert 'obtain renewed approval before publishing' in refused['message']
    assert h.detail(w)['workspace']['status'] == 'approved'
    assert approvals() == before
    assert h.get('coordinator', '/api/publications')['publications'] == []

    reopened = h.lifecycle(w, 'reopen')['workspace']
    assert h.publish(reopened)['publication_id']
    after = approvals()
    assert all(after[k][key] == row for k, records in before.items() for key, row in records.items())
