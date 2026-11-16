"""Base-SHA authority parity; disabled identities have an explicit authenticated-request ruling."""
from datetime import timedelta
from typing import get_args
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import current_roles, effective_roles
from apps.api.db import transaction
from apps.api.services import access
from apps.api.services.independence import today
from packages.contracts import schemas as s
from tests.conftest import TEST_PASSWORD


# Derive paths from the registries and the resolver's inheritance map, never a second role list.
INHERITANCE_MAP = {role: effective_roles((role,)) for role in s.ROLE_NAMES}
ADMIN_GRANT_ROLES = tuple(role for role in s.ROLE_NAMES if 'admin' in INHERITANCE_MAP[role])
INHERITANCE_PATHS = [(parent, child) for parent in s.ROLE_NAMES
                     for child in s.ROLE_NAMES if child != parent and child in INHERITANCE_MAP[parent]]
COVERAGE = [pytest.param(1, False, id='one-department'), pytest.param(3, False, id='all-three-departments'),
            pytest.param(3, True, id='third-department-with-no-binding')]


def base_authority(db, identity_id, organisation_id):
    # 6b582a569f532154a96cd8fdecf11c58217b0ebb, apps/api/services/access.py:222-225.
    departments = db.scalars(select(m.Department.id).where(m.Department.organisation_id == organisation_id))
    return all('admin' in current_roles(db, identity_id, ident) for ident in departments)


def base_holders(db, organisation_id):
    return {identity.id: identity.display_name for identity in db.scalars(select(m.Identity))
            if base_authority(db, identity.id, organisation_id)}


def assert_parity(db, identity, organisation_id):
    old = base_authority(db, identity.id, organisation_id)
    new = identity.id in access.organisation_settings_holders(db, organisation_id)
    assert old == new, f'Unruled org-settings authority difference: old={old}, new={new}'
    return old


def grant(db, identity_id, department_ids, roles, state, day):
    starts = day + timedelta(days=1) if state == 'future' else day - timedelta(days=2)
    ends = day if state == 'expired' else None
    for department_id in department_ids:
        for role in roles:
            db.add(m.RoleBinding(identity_id=identity_id, department_id=department_id, role=role,
                                starts_on=starts, ends_on=ends, revoked=state == 'revoked'))
    db.flush()


def subject(db, h, roles, state, count, missing):
    departments = [h.seed['department_id']]
    for index in range(1, count):
        row = m.Department(organisation_id=h.seed['organisation_id'], name=f'Parity department {index} (test)')
        db.add(row)
        db.flush()
        departments.append(row.id)
    identity = m.Identity(issuer='local-development', subject='parity-' + m.uid(), display_name='Parity sign-in (test)',
                          password_hash=db.get(m.Identity, h.seed['identities']['staff']).password_hash)
    db.add(identity)
    db.flush()
    grant(db, identity.id, departments[:-1] if missing else departments, roles, state, today(db, departments[0]))
    return identity, departments


def test_parity_paths_cover_current_role_and_inheritance_registries():
    assert set(INHERITANCE_MAP) == set(s.ROLE_NAMES)
    assert s.EXPLICIT_ROLES <= set(s.ROLE_NAMES)
    assert ADMIN_GRANT_ROLES
    assert {role for role in get_args(s.ReviewerRole)} <= set(s.ROLE_NAMES)


@pytest.mark.parametrize('role', s.ROLE_NAMES)
@pytest.mark.parametrize('state', ['active', 'expired', 'future', 'revoked'])
@pytest.mark.parametrize('count,missing', COVERAGE)
def test_every_explicit_role_binding_path_matches_base_authority(h, role, state, count, missing):
    with transaction(h.app.state.engine, write=True) as db:
        identity, _ = subject(db, h, (role,), state, count, missing)
        answer = assert_parity(db, identity, h.seed['organisation_id'])
        assert answer == ('admin' in INHERITANCE_MAP[role] and state == 'active' and not missing)


@pytest.mark.parametrize('parent,child', INHERITANCE_PATHS)
@pytest.mark.parametrize('reverse', [False, True], ids=['parent-implies-child', 'child-does-not-elevate-parent'])
@pytest.mark.parametrize('count,missing', COVERAGE)
def test_registry_inheritance_and_reverse_paths_match_base(h, parent, child, reverse, count, missing):
    role = child if reverse else parent
    with transaction(h.app.state.engine, write=True) as db:
        identity, departments = subject(db, h, (role,), 'active', count, missing)
        assert current_roles(db, identity.id, departments[0]) == INHERITANCE_MAP[role]
        assert assert_parity(db, identity, h.seed['organisation_id']) == ('admin' in INHERITANCE_MAP[role] and not missing)


@pytest.mark.parametrize('admin_role', ADMIN_GRANT_ROLES)
@pytest.mark.parametrize('explicit_role', sorted(s.EXPLICIT_ROLES))
@pytest.mark.parametrize('count,missing', COVERAGE)
def test_system_roles_are_explicit_and_do_not_change_admin_authority(h, admin_role, explicit_role, count, missing):
    assert explicit_role not in INHERITANCE_MAP[admin_role]
    with transaction(h.app.state.engine, write=True) as db:
        identity, departments = subject(db, h, (admin_role, explicit_role), 'active', count, missing)
        assert explicit_role in current_roles(db, identity.id, departments[0])
        assert assert_parity(db, identity, h.seed['organisation_id']) == (not missing)


@pytest.mark.parametrize('count,missing', COVERAGE)
def test_all_non_admin_registry_roles_together_do_not_elevate_to_admin(h, count, missing):
    roles = tuple(role for role in s.ROLE_NAMES if role not in ADMIN_GRANT_ROLES)
    with transaction(h.app.state.engine, write=True) as db:
        identity, _ = subject(db, h, roles, 'active', count, missing)
        assert assert_parity(db, identity, h.seed['organisation_id']) is False


@pytest.mark.parametrize('review_role', get_args(s.ReviewerRole))
@pytest.mark.parametrize('state', ['active', 'expired', 'future'])
@pytest.mark.parametrize('admin_role', [None, *ADMIN_GRANT_ROLES], ids=lambda role: role or 'no-admin-binding')
@pytest.mark.parametrize('count,missing', COVERAGE)
def test_review_delegations_never_add_org_settings_authority(h, review_role, state, admin_role, count, missing):
    roles = tuple(dict.fromkeys((admin_role, review_role))) if admin_role else (review_role,)
    with transaction(h.app.state.engine, write=True) as db:
        identity, departments = subject(db, h, roles, 'active', count, missing)
        route = m.ReviewRoute(department_id=departments[0], name='Parity review route',
            starts_on=today(db, departments[0]) - timedelta(days=1), status='active',
            created_by=h.seed['identities']['admin'], activated_by=h.seed['identities']['approver'],
            definition=s.ReviewRouteDefinition(steps=[s.ReviewStep(key='parity', label='Parity', role=review_role)]).model_dump(mode='json'))
        db.add(route)
        db.flush()
        moment = m.now()
        starts = moment + timedelta(hours=1) if state == 'future' else moment - timedelta(hours=2)
        expires = moment - timedelta(hours=1) if state == 'expired' else moment + timedelta(hours=2)
        db.add(m.ReviewDelegation(department_id=departments[0], route_id=route.id, route_revision=route.revision,
            step_key='parity', delegator_id=h.seed['identities']['admin'], delegate_id=identity.id,
            starts_at=starts, expires_at=expires, reason='Synthetic parity delegation'))
        db.flush()
        assert assert_parity(db, identity, h.seed['organisation_id']) == (admin_role is not None and not missing)


@pytest.mark.parametrize('admin_role', ADMIN_GRANT_ROLES)
@pytest.mark.parametrize('placement', ['primary', 'linked-login', 'split-across-logins'])
def test_linked_login_identities_do_not_pool_admin_bindings(h, admin_role, placement):
    with transaction(h.app.state.engine, write=True) as db:
        identity, departments = subject(db, h, (), 'active', 3, False)
        identity.person_id = h.seed['people'][0]
        alias = m.Identity(issuer='local-development', subject='linked-' + m.uid(), display_name='Linked parity sign-in (test)',
                           person_id=identity.person_id)
        db.add(alias)
        db.flush()
        day = today(db, departments[0])
        if placement == 'primary':
            grant(db, identity.id, departments, (admin_role,), 'active', day)
        elif placement == 'linked-login':
            grant(db, alias.id, departments, (admin_role,), 'active', day)
        else:
            grant(db, identity.id, departments[:1], (admin_role,), 'active', day)
            grant(db, alias.id, departments[1:], (admin_role,), 'active', day)
        assert assert_parity(db, identity, h.seed['organisation_id']) == (placement == 'primary')
        assert assert_parity(db, alias, h.seed['organisation_id']) == (placement == 'linked-login')


@pytest.mark.parametrize('admin_role', ADMIN_GRANT_ROLES)
def test_disabled_identity_is_an_intended_raw_difference_but_http_refuses_under_both_rules(h, monkeypatch, admin_role):
    # maintenance ruling: authenticated-request parity; retain and name the raw difference, never skip it.
    with transaction(h.app.state.engine, write=True) as db:
        identity, departments = subject(db, h, (admin_role,), 'active', 3, False)
        identity_id, username = identity.id, identity.subject
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        client.headers['origin'] = h.settings.public_origin
        signed_in = client.post('/auth/login', json={'username': username, 'password': TEST_PASSWORD,
                                                    'department_id': departments[0]})
        assert signed_in.status_code == 200, signed_in.text
        client.headers['x-csrf-token'] = client.cookies['rota_csrf']
        me = client.get('/api/me').json()
        department, organisation = me['department'], me['organisation']
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Identity, identity_id).disabled = True
        with transaction(h.app.state.engine) as db:
            assert base_authority(db, identity_id, organisation['id']) is True
            assert identity_id not in access.organisation_settings_holders(db, organisation['id'])
        value = {'department_name': department['name'], 'department_revision': department['revision'],
            'organisation_name': organisation['name'] + ' changed', 'timezone': organisation['timezone'],
            'week_start': organisation['week_start'], 'reason': 'Prove disabled sign-in cannot reach either authority rule'}
        for resolver in (base_holders, access.organisation_settings_holders):
            called = Mock(wraps=resolver)
            with monkeypatch.context() as patch:
                patch.setattr(access, 'organisation_settings_holders', called)
                response = client.post('/api/commands', headers={'idempotency-key': m.uid()},
                    json={'kind': 'department.settings', 'target_id': department['id'],
                          'expected_revision': organisation['revision'], 'payload': value})
                assert response.status_code == 401, response.text
                assert response.json()['error']['code'] == 'ACCESS_REVOKED'
                called.assert_not_called()
