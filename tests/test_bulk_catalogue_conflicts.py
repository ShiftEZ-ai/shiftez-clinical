"""A pending catalogue write must participate in the next item's existing checks."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.common import CATALOGUES
from .governance_support import identity
from .test_bulk import preview


def creation_values(h, catalogue, conflict):
    if catalogue == 'people':
        return [{'display_name': name, 'short_code': 'PEND01'} for name in ('Pending person one', 'Pending person two')]
    if catalogue == 'categories':
        return [{'name': name, 'short_code': 'PEND01', 'starts_on': '2026-01-01'}
                for name in ('Pending rank one', 'Pending rank two')]
    if catalogue == 'leave_types':
        if conflict == 'name':
            return [{'name': 'Pending duplicate leave', 'short_code': code} for code in ('PEND01', 'PEND02')]
        if conflict == 'case_name':
            return [{'name': name, 'short_code': code} for name, code in (
                ('Pending duplicate leave', 'PEND01'), ('pending duplicate leave', 'PEND02'))]
        return [{'name': name, 'short_code': 'PEND01'} for name in ('Pending leave one', 'Pending leave two')]
    if catalogue == 'programme_memberships':
        programme = h.configure('programmes', {'name': 'Pending membership programme', 'starts_on': '2026-01-01',
                                             'ends_on': '2027-01-01'})['record']
        return [{'person_id': h.seed['people'][0], 'programme_id': programme['id'],
                 'starts_on': start, 'ends_on': '2026-12-01'} for start in ('2026-10-01', '2026-11-01')]
    return [{'person_id': h.seed['people'][0], 'mentor_id': mentor, 'starts_on': '2026-10-01',
             'ends_on': '2026-12-01'} for mentor in h.seed['people'][1:3]]


def assert_atomic_conflict(h, catalogue, payload, code, status):
    cls = CATALOGUES[catalogue]
    with transaction(h.app.state.engine) as db:
        before = set(db.scalars(select(cls.id)))
        histories = set(db.scalars(select(m.DefinitionHistory.id)))
    response = h.client('admin').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': payload})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert [(row['result'], [issue['code'] for issue in row['issues']]) for row in preview['items']] == [
        ('ready', []), ('problem', [code])]
    with transaction(h.app.state.engine) as db:
        assert set(db.scalars(select(cls.id))) == before
        assert set(db.scalars(select(m.DefinitionHistory.id))) == histories
    refused = h.command('admin', 'bulk.apply', payload, status=status)['error']
    assert refused['code'] == code
    assert refused['details']['failed_index'] == 1 and refused['details']['completed'] == 0
    with transaction(h.app.state.engine) as db:
        assert set(db.scalars(select(cls.id))) == before
        assert set(db.scalars(select(m.DefinitionHistory.id))) == histories


@pytest.mark.parametrize('catalogue,conflict,code,status', [
    ('people', 'code', 'SHORT_CODE_TAKEN', 409),
    ('categories', 'code', 'RANK_CODE_TAKEN', 409),
    ('leave_types', 'code', 'LEAVE_TYPE_TAKEN', 409),
    ('leave_types', 'name', 'LEAVE_TYPE_TAKEN', 409),
    ('leave_types', 'case_name', 'LEAVE_TYPE_TAKEN', 409),
    ('programme_memberships', 'overlap', 'DUPLICATE_RELATIONSHIP', 409),
    ('mentorships', 'overlap', 'DUPLICATE_RELATIONSHIP', 409),
])
def test_bulk_catalogue_creations_check_prior_pending_items(h, catalogue, conflict, code, status):
    values = creation_values(h, catalogue, conflict)
    payload = {'item_kind': 'catalogue.create', 'common': {'catalogue': catalogue},
               'items': [{'payload': {'value': value}} for value in values], 'reason': 'Conflicting pending catalogue fixture'}
    assert_atomic_conflict(h, catalogue, payload, code, status)


@pytest.mark.parametrize('catalogue,code', [('people', 'SHORT_CODE_TAKEN'), ('leave_types', 'LEAVE_TYPE_TAKEN')])
def test_bulk_catalogue_updates_check_prior_unflushed_codes(h, catalogue, code):
    rows = h.get('admin', '/api/catalogues')[catalogue][:2]
    payload = {'item_kind': 'catalogue.update', 'common': {'catalogue': catalogue}, 'items': [
        {'target_id': row['id'], 'expected_revision': row['revision'],
         'changes': [{'field': 'short_code', 'value': 'PEND01'}]} for row in rows],
        'reason': 'Conflicting pending short codes'}
    with transaction(h.app.state.engine) as db:
        before = {row.id: row.short_code for row in db.scalars(select(CATALOGUES[catalogue]))}
    assert_atomic_conflict(h, catalogue, payload, code, 409)
    with transaction(h.app.state.engine) as db:
        assert {row.id: row.short_code for row in db.scalars(select(CATALOGUES[catalogue]))} == before


def test_legacy_catalogue_bulk_undo_restores_an_attested_warning_and_rejects_tampering(h):
    person_id = h.seed['people'][0]
    h.command('coordinator', 'catalogue.bulk_update', {'catalogue': 'people',
        'items': [{'id': person_id, 'revision': 1}], 'add_tags': ['LegacyAffected'], 'reason': 'Original catalogue fixture'})
    # Safety kinds are always mandatory. A workload cap still yields an attested warning.
    h.policy({'kind': 'max_duties', 'maximum': 0}, {'tags': ['LegacyAffected']}, severity='advisory')
    workspace = h.shift(h.workspace())['workspace']
    from packages.domain.validation import evaluate
    original = evaluate(h.snapshot(workspace))
    assert original['warning_count'] > 0
    person = next(row for row in h.get('coordinator', '/api/catalogues')['people'] if row['id'] == person_id)
    forward = h.command('coordinator', 'catalogue.bulk_update', {'catalogue': 'people',
        'items': [{'id': person_id, 'revision': person['revision']}], 'remove_tags': ['LegacyAffected'],
        'reason': 'Remove warning scope'})
    assert evaluate(h.snapshot(workspace))['warning_count'] < original['warning_count']
    inverse = forward['undo']['payload']
    assert inverse['undo_of'] == forward['receipt_id']
    ordinary = deepcopy(inverse)
    ordinary.pop('undo_of')
    refused = h.command('coordinator', 'bulk.apply', ordinary, status=409)['error']
    assert refused['code'] == 'MAX_DUTIES'
    tampered = deepcopy(inverse)
    tampered['items'][0]['payload']['value']['title'] = 'Injected by a modified inverse'
    refused = h.command('coordinator', 'bulk.apply', tampered, status=409)['error']
    assert refused['code'] == 'BULK_UNDO_CHANGED'
    identity(h, 'another_catalogue_coordinator', ['coordinator'])
    refused = h.command('another_catalogue_coordinator', 'bulk.apply', inverse, status=403)['error']
    assert refused['code'] == 'FORBIDDEN'
    checked = preview(h, 'coordinator', inverse)
    assert checked['counts']['ready'] == 1
    assert any(issue['code'] == 'MAX_DUTIES' for issue in checked['items'][0]['issues'])
    result = h.command('coordinator', 'bulk.apply', inverse)
    with transaction(h.app.state.engine) as db:
        receipt = db.get(m.CommandReceipt, result['receipt_id'])
        assert any(issue['code'] == 'MAX_DUTIES' for issue in receipt.result['items'][0]['issues'])
    restored = next(row for row in h.get('coordinator', '/api/catalogues')['people'] if row['id'] == person_id)
    assert restored['tags'] == person['tags'] and restored['title'] == person['title']
    assert evaluate(h.snapshot(workspace))['warning_count'] == original['warning_count']
