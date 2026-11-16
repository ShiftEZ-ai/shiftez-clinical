"""Nested actions share the affected-record ceiling with ordinary bulk items."""
import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .test_bulk import preview


def limits(h, maximum):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).bulk_settings = {
            'bulk_max_items': maximum, 'bulk_direct_max': maximum}


def counts(h):
    with transaction(h.app.state.engine) as db:
        return [db.scalar(select(func.count()).select_from(cls))
                for cls in (m.Membership, m.DefinitionHistory, m.AuditEvent, m.CommandReceipt)]


@pytest.mark.parametrize('placement', ['item', 'common'])
def test_nested_membership_bulk_create_cannot_exceed_affected_record_limit(h, placement):
    limits(h, 1)
    category = h.configure('categories', {'name': 'Synthetic nested rank', 'starts_on': '2020-01-01'})['record']['id']
    action = {'item_kind': 'membership.bulk_create', 'payload': {
        'person_ids': h.seed['people'][:2], 'category_id': category, 'starts_on': '2027-01-01'}}
    body = {'items': [{}], 'reason': 'Give these colleagues the selected rank'}
    (body['items'][0] if placement == 'item' else body)['changes'] = [action]
    client = h.client('coordinator')
    before = counts(h)
    checked = client.post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': body})
    assert checked.status_code == 422, checked.text
    assert checked.json()['error']['code'] == 'BULK_ITEM_LIMIT'
    assert counts(h) == before
    refused = h.command('coordinator', 'bulk.apply', body, status=422)
    assert refused['error']['code'] == 'BULK_ITEM_LIMIT'
    assert counts(h) == before


def test_nested_membership_bulk_create_accepts_the_exact_record_limit(h):
    limits(h, 2)
    category = h.configure('categories', {'name': 'Synthetic exact-limit rank', 'starts_on': '2020-01-01'})['record']['id']
    body = {'items': [{'changes': [{'item_kind': 'membership.bulk_create', 'payload': {
        'person_ids': h.seed['people'][:2], 'category_id': category, 'starts_on': '2027-01-01'}}]}],
        'reason': 'Give exactly two colleagues the selected rank'}
    h.client('coordinator')
    before = counts(h)
    assert preview(h, 'coordinator', body)['items'][0]['result'] == 'ready'
    assert counts(h) == before
    result = h.command('coordinator', 'bulk.apply', body)
    assert result['items'][0]['result'] == 'changed'
    assert counts(h)[0] == before[0] + 2


@pytest.mark.parametrize('kind, payload', [
    ('eligibility.bulk_create', {'person_ids': ['a', 'b'], 'service_ids': ['x', 'y']}),
    ('grant.bulk_create', {'person_ids': ['a', 'b'], 'capability_ids': ['x', 'y']}),
    ('eligibility.programme', {'person_ids': ['a', 'b'], 'service_ids': ['x', 'y']}),
    ('catalogue.bulk_update', {'items': [{}, {}]}),
    ('request.decide_many', {'items': [{}, {}]}),
    ('policy.activate_many', {'items': [{}, {}]}),
    ('exception.create_many', {'items': [{}, {}]}),
    ('shift.action', {'shift_ids': ['a', 'b']}),
    ('shift.copy', {'shift_ids': ['a', 'b']}),
    ('issues.resolve', {'keys': ['a', 'b']}),
    ('rule.propose', {'add_lines': [{}, {}]}),
])
def test_nested_aggregate_footprints_are_checked_before_dispatch(h, kind, payload):
    limits(h, 1)
    body = {'items': [{'changes': [{'item_kind': kind, 'payload': payload}]}],
            'reason': 'Count every affected record before dispatch'}
    response = h.client('admin').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': body})
    assert response.status_code == 422, response.text
    assert response.json()['error']['code'] == 'BULK_ITEM_LIMIT'


def test_registry_action_defaults_and_field_changes_contribute_to_record_limit(h, monkeypatch):
    limits(h, 2)
    monkeypatch.setitem(s.BULK_COLLECTIONS['people']['actions'], 'give_ranks', {
        'kind': 'membership.bulk_create', 'payload': {'person_ids': h.seed['people'][:2]}})
    person = h.get('coordinator', '/api/catalogues')['people'][0]
    body = {'item_kind': 'catalogue.update', 'common': {'catalogue': 'people'},
        'items': [{'target_id': person['id'], 'expected_revision': person['revision'], 'changes': [
            {'field': 'title', 'how': 'set', 'value': 'Synthetic title'},
            {'action': 'give_ranks', 'value': {'category_id': h.seed['category'], 'starts_on': '2027-01-01'}}]}],
        'reason': 'Count the person update and two related records'}
    response = h.client('coordinator').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': body})
    assert response.status_code == 422, response.text
    assert response.json()['error']['code'] == 'BULK_ITEM_LIMIT'
    assert h.get('coordinator', '/api/catalogues')['people'][0] == person


@pytest.mark.parametrize('person_ids', [None, 'not-a-list'])
def test_malformed_nested_aggregate_is_the_handlers_row_issue(h, person_ids):
    body = {'items': [{'changes': [{'item_kind': 'membership.bulk_create', 'payload': {
        'person_ids': person_ids, 'category_id': h.seed['category'], 'starts_on': '2027-01-01'}}]}],
        'reason': 'Validate an incomplete nested rank action'}
    checked = preview(h, 'coordinator', body)
    assert checked['items'][0]['result'] == 'problem'
    assert checked['items'][0]['issues'][0]['code'] == 'SCHEMA_INVALID'
    assert checked['items'][0]['issues'][0]['message'] == 'Input should be a valid list'
