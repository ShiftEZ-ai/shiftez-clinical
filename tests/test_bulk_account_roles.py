"""Account role filters select sign-ins, without borrowing another sign-in's roles."""
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity


REASON = 'Synthetic account-specific role selection'


def accounts(h):
    person = h.seed['people'][0]
    ids = {
        'unlinked_lead': identity(h, 'unlinked-role-lead', ['programme_lead']),
        'unlinked_staff': identity(h, 'unlinked-role-staff', ['staff']),
        'linked_lead': identity(h, 'linked-role-lead', ['programme_lead'], person=person),
        'linked_staff': identity(h, 'linked-role-staff', ['staff'], person=person),
        'revoked_lead': identity(h, 'revoked-role-lead', ['programme_lead']),
        'future_lead': identity(h, 'future-role-lead', ['programme_lead'], person=person),
        'ended_lead': identity(h, 'ended-role-lead', ['programme_lead'], person=person),
        'without_roles': identity(h, 'without-account-roles', []),
    }
    with transaction(h.app.state.engine, write=True) as db:
        for name in ('revoked_lead', 'future_lead', 'ended_lead'):
            binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == ids[name]))
            if name == 'revoked_lead':
                binding.revoked = True
            elif name == 'future_lead':
                binding.starts_on = date(2026, 10, 5)
            else:
                binding.ends_on = date(2026, 10, 4)
    return ids


def role_selection(ids, mode, *, collection='identities'):
    where = {'as_of': '2026-10-04', 'filters': {'id': list(ids)}}
    if mode == 'flat':
        where['filters']['roles'] = ['programme_lead']
    else:
        where['conditions'] = {'match': 'all', 'conditions': [
            {'field': 'roles', 'op': 'any', 'values': ['programme_lead']}]}
    return {'collection': collection, 'where': where}


@pytest.mark.parametrize('mode', ['flat', 'conditions'])
def test_account_role_selection_and_disable_use_each_sign_ins_own_roles(h, mode):
    ids = accounts(h)
    expected = {ids['unlinked_lead'], ids['linked_lead']}
    selection = role_selection(ids.values(), mode)
    response = h.client('admin').post('/api/bulk/resolve', json=selection)
    assert response.status_code == 200, response.text
    resolved = response.json()
    assert resolved['count'] == 2 and {r['id'] for r in resolved['sample']} == expected
    paged = []
    for offset in (0, 1):
        response = h.client('admin').post('/api/bulk/rows', json={
            'selection': selection, 'columns': ['display_name', 'person_id'], 'offset': offset, 'limit': 1})
        assert response.status_code == 200, response.text
        page = response.json()
        assert page['count'] == 2 and page['token'] == resolved['token']
        paged.extend(row['id'] for row in page['rows'])
    assert set(paged) == expected

    payload = {'item_kind': 'identity.disable', 'selection': selection, 'reason': REASON}
    preview = h.client('admin').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': payload})
    assert preview.status_code == 200, preview.text
    assert preview.json()['counts']['ready'] == 2
    assert {row['target_id'] for row in preview.json()['items']} == expected
    response = h.client('admin').post('/api/commands', headers={'idempotency-key': str(uuid4())},
        json={'kind': 'bulk.apply', 'payload': payload,
              'expected_fingerprint': preview.json()['source_fingerprint']})
    assert response.status_code == 200, response.text
    assert response.json()['count'] == 2
    with transaction(h.app.state.engine) as db:
        disabled = {row.id for row in db.scalars(select(m.Identity).where(
            m.Identity.id.in_(ids.values()), m.Identity.disabled.is_(True)))}
        assert disabled == expected


@pytest.mark.parametrize('mode', ['flat', 'conditions'])
def test_people_role_selection_keeps_union_of_a_persons_sign_ins(h, mode):
    ids = accounts(h)
    selection = role_selection(h.seed['people'], mode, collection='people')
    response = h.client('admin').post('/api/bulk/resolve', json=selection)
    assert response.status_code == 200, response.text
    resolved = response.json()
    assert resolved['count'] == 1 and resolved['sample'][0]['id'] == h.seed['people'][0]
    assert ids['unlinked_lead'] not in {r['id'] for r in resolved['sample']}


@pytest.mark.parametrize('mode', ['flat', 'conditions'])
def test_role_binding_filters_use_the_binding_identity_not_a_shared_person(h, mode):
    ids = accounts(h)
    with transaction(h.app.state.engine) as db:
        bindings = list(db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id.in_(ids.values()))))
        expected = {row.id for row in bindings if row.identity_id in {ids['unlinked_lead'], ids['linked_lead']}}
        binding_ids = [row.id for row in bindings]
    selection = role_selection(binding_ids, mode, collection='roles')
    response = h.client('admin').post('/api/bulk/resolve', json=selection)
    assert response.status_code == 200, response.text
    assert response.json()['count'] == 2 and {row['id'] for row in response.json()['sample']} == expected


@pytest.mark.parametrize('shape', ['common', 'item', 'operation', 'selection'])
def test_bulk_give_a_role_refuses_admin_but_individual_grants_remain_available(h, shape):
    target = h.seed['identities']['staff']
    role = {'identity_id': target, 'role': 'admin', 'starts_on': '2027-01-01'}
    action = {'item_kind': 'role.grant', 'payload': role}
    if shape == 'common':
        payload = {'item_kind': 'role.grant', 'common': role, 'items': [{}]}
    elif shape == 'item':
        payload = {'items': [action]}
    elif shape == 'operation':
        payload = {'items': [{'changes': [action]}]}
    else:
        with transaction(h.app.state.engine) as db:
            binding = db.scalar(select(m.RoleBinding.id).where(m.RoleBinding.identity_id == target))
        payload = {'selection': {'collection': 'roles', 'include_ids': [binding]}, 'changes': [action]}
    payload['reason'] = REASON
    response = h.client('admin').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': payload})
    assert response.status_code == 200, response.text
    check = response.json()
    assert check['counts']['problem'] == 1
    issue = check['items'][0]['issues'][0]
    assert issue['code'] == 'BULK_ADMIN_ROLE'
    assert issue['message'] == 'Give Admin access individually through Access'
    response = h.client('admin').post('/api/commands', headers={'idempotency-key': str(uuid4())},
        json={'kind': 'bulk.apply', 'payload': payload, 'expected_fingerprint': check['source_fingerprint']})
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'BULK_ADMIN_ROLE'
    with transaction(h.app.state.engine) as db:
        assert not list(db.scalars(select(m.RoleBinding).where(
            m.RoleBinding.identity_id == target, m.RoleBinding.role == 'admin')))
    result = h.command('admin', 'role.grant', {**role, 'reason': REASON})
    assert result['record']['role'] == 'admin'
