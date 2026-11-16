"""Bulk retirement reuses governed per-row commands, one audited ChangeSet and one served Undo."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .test_bulk import preview
from .test_future_record_withdrawal import REASON, _rank, _eligibility, _programme
from .test_rotation_withdrawal import future_value


@pytest.mark.parametrize('collection,make,role,cls,action', [
    ('memberships', _rank, 'coordinator', m.Membership, 'withdraw'),
    ('eligibilities', _eligibility, 'credentialer', m.Eligibility, 'withdraw'),
    ('programme_memberships', _programme, 'coordinator', m.ProgrammeMembership, 'withdraw'),
    ('rotations', None, 'coordinator', m.Rotation, 'remove')])
def test_bulk_withdrawal_previews_rolls_back_and_restores_one_changeset(h, collection, make, role, cls, action):
    if make:
        first = make(h, '2028-02-01')
        if collection == 'memberships':
            first = h.command(role, 'membership.update_dates', {'ends_on': '2028-02-02', 'reason': REASON},
                              first['id'], first['revision'])['record']
        second = make(h, '2028-03-01')
    else:
        value = future_value(h)
        first = h.configure('rotations', {**value, 'ends_on': '2026-12-15'}, role=role)['record']
        second = h.configure('rotations', {**value, 'starts_on': '2026-12-15'}, role=role)['record']
    descriptor = s.BULK_COLLECTIONS[collection]['actions'][action]
    assert descriptor['destructive'] and descriptor['fields'] == {}
    payload = {'common': {'catalogue': collection}, 'reason': REASON,
        'items': [{'target_id': row['id'], 'expected_revision': row['revision'],
                   'changes': [{'action': action}]} for row in (first, second)]}
    check = preview(h, role, payload)
    assert check['counts']['ready'] == 2, check
    with transaction(h.app.state.engine) as db:
        assert all(db.get(cls, row['id']) is not None for row in (first, second))
    stale = {**payload, 'items': [payload['items'][0], {**payload['items'][1], 'expected_revision': 999}]}
    h.command(role, 'bulk.apply', stale, status=409)
    with transaction(h.app.state.engine) as db:
        assert all(not db.get(cls, row['id']).withdrawn for row in (first, second))
    result = h.command(role, 'bulk.apply', payload)
    assert result['count'] == 2 and result['counts']['changed'] == 2
    assert result['undo']['kind'] == 'bulk.apply'
    with transaction(h.app.state.engine) as db:
        for row in (first, second):
            assert db.get(cls, row['id']) is None
            assert db.get(cls, row['id'], execution_options={'include_withdrawn': True}).withdrawn
        versions = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.batch_id == result['batch_id'])))
        assert len(versions) == 2 and all(v.snapshot['withdrawn'] for v in versions)
    restored = h.command(role, 'bulk.apply', result['undo']['payload'])
    assert restored['counts']['changed'] == 2
    with transaction(h.app.state.engine) as db:
        assert all(not db.get(cls, row['id']).withdrawn for row in (first, second))


def test_bulk_withdrawal_keeps_per_row_permission(h):
    row = _eligibility(h, '2028-02-01')
    value = {'item_kind': 'eligibility.withdraw', 'reason': REASON,
             'items': [{'target_id': row['id'], 'expected_revision': row['revision']}]}
    assert preview(h, 'coordinator', value)['items'][0]['issues'][0]['code'] == 'FORBIDDEN'
    h.command('coordinator', 'bulk.apply', value, status=403)


def test_bulk_rotation_refuses_payload_target_substitution(h):
    row = h.configure('rotations', future_value(h), role='coordinator')['record']
    value = {'item_kind': 'rotation.remove', 'reason': REASON, 'items': [{
        'target_id': row['id'], 'expected_revision': row['revision'],
        'payload': {'items': [{'id': row['id'], 'revision': row['revision']}]}}]}
    assert preview(h, 'coordinator', value)['items'][0]['issues'][0]['code'] == 'BULK_TARGET_MISMATCH'


def test_bulk_rotation_reports_the_existing_request_guard(h):
    row = h.configure('rotations', future_value(h), role='coordinator')['record']
    h.command('staff', 'rotation.request_change', {'rotation_id': row['id'],
        'rotation_type_id': row['rotation_type_id'], 'starts_on': '2026-12-02',
        'ends_on': row['ends_on'], 'private_reason': REASON})
    value = {'item_kind': 'rotation.remove', 'reason': REASON,
             'items': [{'target_id': row['id'], 'expected_revision': row['revision']}]}
    assert preview(h, 'coordinator', value)['items'][0]['issues'][0]['code'] == 'ROTATION_HAS_REQUEST'
    h.command('coordinator', 'bulk.apply', value, status=409)
    with transaction(h.app.state.engine) as db:
        assert not db.get(m.Rotation, row['id']).withdrawn


def test_bulk_can_remove_and_replace_rotation_with_one_undo(h):
    value = future_value(h)
    row = h.configure('rotations', value, role='coordinator')['record']
    result = h.command('coordinator', 'bulk.apply', {'reason': REASON, 'items': [
        {'item_kind': 'rotation.remove', 'target_id': row['id'], 'expected_revision': row['revision']},
        {'item_kind': 'catalogue.create', 'payload': {'catalogue': 'rotations', 'value': value}}]})
    assert result['counts']['changed'] == 2
    h.command('coordinator', 'bulk.apply', result['undo']['payload'])
    with transaction(h.app.state.engine) as db:
        assert [r.id for r in db.scalars(select(m.Rotation))] == [row['id']]
