"""Indirect account links retain person conditions without sharing sign-in roles."""
from uuid import uuid4

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity


def post(h, endpoint, payload, role='admin'):
    response = h.client(role).post('/api/bulk/' + endpoint, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def people_and_accounts(h):
    people = [h.configure('people', {'display_name': name})['record']['id']
              for name in ('Synthetic Alex', 'Synthetic Blair')]
    rank = h.configure('categories', {'name': 'Synthetic linked rank', 'starts_on': '2020-01-01'})['record']['id']
    programme = h.configure('programmes', {'name': 'Synthetic linked programme', 'starts_on': '2020-01-01'})['record']['id']
    h.command('admin', 'membership.create', {'person_id': people[0], 'category_id': rank,
        'starts_on': '2026-10-04', 'ends_on': '2026-11-01'})
    h.configure('programme_memberships', {'person_id': people[0], 'programme_id': programme,
        'starts_on': '2026-10-04', 'ends_on': '2026-11-01'})
    ids = [identity(h, 'linked-alex-staff', ['staff'], person=people[0]),
           identity(h, 'linked-alex-lead', ['programme_lead'], person=people[0]),
           identity(h, 'linked-blair-staff', ['staff'], person=people[1]),
           identity(h, 'unlinked-staff', ['staff'])]
    return people, {'person_ids': people[0], 'category_ids': rank, 'programme_ids': programme}, ids


@pytest.mark.parametrize('field', ['person_ids', 'category_ids', 'programme_ids'])
@pytest.mark.parametrize('op', ['any', 'none'])
def test_role_binding_person_conditions_resolve_page_and_revoke_only_matching_accounts(h, field, op):
    people, values, accounts = people_and_accounts(h)
    with transaction(h.app.state.engine) as db:
        bindings = list(db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id.in_(accounts))))
        expected = {row.id for row in bindings if (row.identity_id in accounts[:2]) == (op == 'any')}
        by_id = {row.id: row.identity_id for row in bindings}
    selection = {'collection': 'roles', 'where': {'as_of': '2026-10-04',
        'filters': {'id': list(by_id)},
        'conditions': {'conditions': [{'field': field, 'op': op, 'values': [values[field]]}]}}}
    resolved = post(h, 'resolve', selection)
    assert resolved['count'] == len(expected) and {r['id'] for r in resolved['sample']} == expected
    assert {r['person_id'] for r in resolved['sample']} == ({people[0]} if op == 'any' else {people[1], None})
    found = []
    for offset in range(len(expected) + 1):
        page = post(h, 'rows', {'selection': selection, 'columns': ['identity_id', 'person_id'],
            'sort': [{'field': 'person_id'}], 'group': 'person_id', 'offset': offset, 'limit': 1})
        assert page['count'] == len(expected)
        found.extend(r['id'] for r in page['rows'])
    assert set(found) == expected and len(found) == len(expected)
    payload = {'item_kind': 'role.revoke', 'selection': selection, 'reason': 'Revoke only matching synthetic access'}
    preview = h.client('admin').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': payload})
    assert preview.status_code == 200, preview.text
    assert preview.json()['counts']['ready'] == len(expected)
    assert {r['target_id'] for r in preview.json()['items']} == expected
    applied = h.client('admin').post('/api/commands', headers={'idempotency-key': str(uuid4())}, json={
        'kind': 'bulk.apply', 'payload': payload, 'expected_fingerprint': preview.json()['source_fingerprint']})
    assert applied.status_code == 200, applied.text
    with transaction(h.app.state.engine) as db:
        assert {r.id for r in db.scalars(select(m.RoleBinding).where(
            m.RoleBinding.id.in_(by_id), m.RoleBinding.revoked.is_(True)))} == expected


def test_role_binding_person_filter_keeps_sign_in_roles_separate_and_respects_as_of(h):
    _, values, accounts = people_and_accounts(h)
    with transaction(h.app.state.engine) as db:
        expected = db.scalar(select(m.RoleBinding.id).where(m.RoleBinding.identity_id == accounts[1]))
    selection = {'collection': 'roles', 'where': {'as_of': '2026-10-04', 'filters': {
        'category_ids': [values['category_ids']], 'roles': ['programme_lead']}}}
    assert {r['id'] for r in post(h, 'resolve', selection)['sample']} == {expected}
    assert {r['id'] for r in post(h, 'rows', {'selection': selection})['rows']} == {expected}
    for day in ('2026-10-03', '2026-11-01'):
        selection['where']['as_of'] = day
        assert post(h, 'resolve', selection)['count'] == post(h, 'rows', {'selection': selection})['count'] == 0


@pytest.mark.parametrize('collection', ['roles', 'inbox'])
@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_indirect_person_projection_and_cached_filters_follow_account_relink(request, database, collection):
    h = request.getfixturevalue(database)
    people, _, accounts = people_and_accounts(h)
    with transaction(h.app.state.engine, write=True) as db:
        if collection == 'roles':
            record_id = db.scalar(select(m.RoleBinding.id).where(m.RoleBinding.identity_id == accounts[0]))
        else:
            event = m.Outbox(department_id=h.seed['department_id'], event_key='synthetic-linked-inbox', kind='synthetic', payload={})
            db.add(event)
            db.flush()
            notification = m.Notification(outbox_id=event.id, identity_id=accounts[0], title='Synthetic inbox', message='Synthetic message')
            db.add(notification)
            db.flush()
            record_id = notification.id
            # Another account's notification must stay invisible before conditions/counting.
            db.add(m.Notification(outbox_id=event.id, identity_id=accounts[2], title='Other inbox', message='Other message'))
    role = 'admin' if collection == 'roles' else 'linked-alex-staff'
    selection = {'collection': collection, 'where': {'conditions': {'conditions': [
        {'field': 'person_ids', 'values': [people[0]]}]}}}
    if collection == 'roles':
        selection['where']['filters'] = {'id': [record_id]}
    payload = {'selection': selection, 'columns': ['person_id']}
    for _ in range(2):
        assert post(h, 'resolve', selection, role)['sample'][0]['person_id'] == people[0]
        page = post(h, 'rows', payload, role)
        assert page['count'] == 1 and page['rows'][0]['id'] == record_id and page['rows'][0]['person_id'] == people[0]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, accounts[0]).person_id = people[1]
    assert post(h, 'resolve', selection, role)['count'] == post(h, 'rows', payload, role)['count'] == 0
    selection['where']['conditions']['conditions'][0]['values'] = [people[1]]
    assert post(h, 'rows', payload, role)['rows'][0]['person_id'] == people[1]
