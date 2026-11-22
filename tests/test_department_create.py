"""Create departments through the real command API, never a deployment endpoint."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.auth import current_roles, held_roles
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import departments
from packages.contracts import schemas as s
from tests.conftest import TEST_PASSWORD


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def department_h(request):
    h = request.getfixturevalue(request.param)
    name_test_administrators(h)
    return h


def name_test_administrators(h):
    with transaction(h.app.state.engine, write=True) as db:
        for role in ('admin', 'coordinator'):
            db.get(m.Identity, h.seed['identities'][role]).display_name += ' (test)'


def payload(h, **changes):
    return {'name': 'Golden scenarios (test)', 'first_admin_identity_id': h.get('admin', '/api/me')['identity_id'],
            'reason': 'Create an isolated test department for golden scenarios', **changes}


def preview(h, value, role='operator', status=200):
    response = h.client(role).post('/api/commands/preview', json={'kind': 'department.create', 'payload': value})
    assert response.status_code == status, response.text
    return response.json()


def token(h, value):
    return preview(h, value)['expected_fingerprint']


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_only_operator_may_create_department(department_h, role):
    h = department_h
    value = payload(h)
    client = h.client('operator')
    fingerprint = token(h, value)
    # Exercise every canonical role, including programme_lead which the legacy seed omits.
    with transaction(h.app.state.engine, write=True) as db:
        operator = db.scalar(select(m.Identity).where(m.Identity.subject == 'operator'))
        for row in db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == operator.id)):
            row.revoked = True
        db.add(m.RoleBinding(identity_id=operator.id, department_id=h.seed['department_id'], role=role,
                             starts_on=date(2020, 1, 1)))
    assert ('department.create' in h.get('operator', '/api/me')['permissions']) == (role in s.SYSTEM_ROLES)
    response = client.post('/api/commands', headers={'idempotency-key': str(uuid4())},
                           json={'kind': 'department.create', 'payload': value, 'expected_fingerprint': fingerprint})
    assert response.status_code == (200 if role in s.SYSTEM_ROLES else 403), response.text
    preview(h, value, status=200 if role in s.SYSTEM_ROLES else 403)
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.Department).where(m.Department.name == value['name'])))) == (
            1 if role in s.SYSTEM_ROLES else 0)


@pytest.mark.parametrize(('environment', 'deployment', 'allowed'), [
    ('production', 'production', False), ('test', 'production', False), ('development', 'production', False),
    ('production', 'development', False), ('production', 'test', False), ('production', '', False),
    ('production', 'unknown', False), ('production', 'dev', True), ('production', 'stg', True), ('production', 'staging', False),
    ('test', 'test', True), ('development', 'development', True),
])
def test_deployment_guard_uses_existing_deployment_identity(department_h, environment, deployment, allowed):
    h = department_h
    value = payload(h)
    h.client('operator')
    # Change only this fixture's frozen settings after login; no live configuration or identities.
    object.__setattr__(h.settings, 'environment', environment)
    object.__setattr__(h.settings, 'deployment', deployment)
    reply = h.command('operator', 'department.create', value, status=200 if allowed else 403,
                      expected_fingerprint=token(h, value) if allowed else None)
    preview(h, value, status=200 if allowed else 403)
    if not allowed:
        assert reply['error']['code'] == 'TEST_DEPLOYMENT_REQUIRED'
        with transaction(h.app.state.engine) as db:
            assert db.scalar(select(m.Department).where(m.Department.name == value['name'])) is None


@pytest.mark.parametrize('name', ['Golden scenarios', 'Golden scenarios (Test)', '(test)', '  (test)  '])
def test_department_name_requires_test_suffix(department_h, name):
    h = department_h
    h.command('operator', 'department.create', payload(h, name=name), status=422)


@pytest.mark.parametrize('reason', ['', '  ', 'ok'])
def test_department_creation_requires_a_reason(department_h, reason):
    h = department_h
    h.command('operator', 'department.create', payload(h, reason=reason), status=422)


def test_creator_cannot_be_the_first_administrator(department_h):
    h = department_h
    operator = h.get('operator', '/api/me')['identity_id']
    reply = h.command('operator', 'department.create', payload(h, first_admin_identity_id=operator), status=409)
    assert reply['error']['code'] == 'DIFFERENT_FIRST_ADMIN'


@pytest.mark.parametrize('mode', ['new', 'existing', 'receipt'])
@pytest.mark.parametrize(('name', 'allowed'), [
    ('Real administrator', False), ('Administrator (Test)', False),
    ('Administrator (test) trailing', False), ('(test)', False), ('Administrator (test)', True),
])
def test_first_administrator_requires_a_test_name_without_writing_rows(department_h, mode, name, allowed):
    h = department_h
    value, key = payload(h), str(uuid4())
    h.client('operator')
    fingerprint = None
    if mode != 'new':
        fingerprint = token(h, value)
        h.command('operator', 'department.create', value, key=key, expected_fingerprint=fingerprint)
    models = (m.Department, m.RoleBinding, m.DefinitionHistory, m.CommandReceipt, m.AuditEvent)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, value['first_admin_identity_id']).display_name = name
    with transaction(h.app.state.engine) as db:
        counts = [db.scalar(select(func.count()).select_from(model)) for model in models]
        revision = db.get(m.Organisation, h.seed['organisation_id']).revision
    if allowed and mode != 'receipt':
        fingerprint = token(h, value)
    if not allowed:
        assert preview(h, value, status=409)['error']['code'] == 'FIRST_ADMIN_NOT_TEST'
    reply = h.command('operator', 'department.create', value,
                      key=key if mode == 'receipt' else None, status=200 if allowed else 409,
                      expected_fingerprint=fingerprint)
    if allowed:
        assert reply['created'] is (mode != 'existing')
    else:
        assert reply['error']['code'] == 'FIRST_ADMIN_NOT_TEST'
        assert '(test)' in reply['error']['message']
        with transaction(h.app.state.engine) as db:
            assert [db.scalar(select(func.count()).select_from(model)) for model in models] == counts
            assert db.get(m.Organisation, h.seed['organisation_id']).revision == revision


def test_another_sign_in_for_the_creator_is_not_an_independent_administrator(department_h):
    h = department_h
    value = payload(h)
    with transaction(h.app.state.engine, write=True) as db:
        for role in ('admin', 'operator'):
            db.scalar(select(m.Identity).where(m.Identity.subject == role)).person_id = h.seed['people'][0]
    reply = h.command('operator', 'department.create', value, status=409)
    assert reply['error']['code'] == 'DIFFERENT_FIRST_ADMIN'


def test_department_creation_permission_is_never_default_allowed(department_h, monkeypatch):
    h = department_h
    value = payload(h)
    monkeypatch.setitem(s.PERMISSIONS, 'department.create', ())
    h.command('operator', 'department.create', value, status=403)


@pytest.mark.parametrize('state', ['missing', 'disabled', 'pending', 'automation', 'other_organisation'])
def test_first_administrator_must_be_an_active_sign_in_in_this_organisation(department_h, state):
    h = department_h
    value = payload(h)
    with transaction(h.app.state.engine, write=True) as db:
        identity = db.get(m.Identity, value['first_admin_identity_id'])
        if state == 'disabled':
            identity.disabled = True
        elif state == 'pending':
            db.add(m.Account(identity_id=identity.id, username='pending-admin', email='pending@example.test'))
        elif state == 'automation':
            identity.issuer = 'fmuc:swap-automation'
        elif state == 'other_organisation':
            organisation = m.Organisation(name='Other test organisation')
            db.add(organisation)
            db.flush()
            department = m.Department(organisation_id=organisation.id, name='Other department (test)')
            db.add(department)
            db.flush()
            db.add(m.RoleBinding(identity_id=identity.id, department_id=department.id, role='admin', starts_on=date(2020, 1, 1)))
        else:
            value['first_admin_identity_id'] = str(uuid4())
    reply = h.command('operator', 'department.create', value, status=409 if state == 'pending' else 404)
    assert reply['error']['code'] == ('FIRST_ADMIN_NOT_ACTIVE' if state == 'pending' else 'NOT_FOUND')


def test_creation_is_audited_atomic_idempotent_and_does_not_grant_the_creator_access(department_h):
    h = department_h
    value = payload(h)
    operator = h.get('operator', '/api/me')['identity_id']
    organisation = h.get('admin', '/api/me')['organisation']
    key = str(uuid4())
    fingerprint = token(h, value)
    created = h.command('operator', 'department.create', value, key=key, expected_fingerprint=fingerprint)
    assert created['created'] is True
    assert h.command('operator', 'department.create', value, key=key, expected_fingerprint=fingerprint) == created
    assert h.committed('operator', key) == created
    variant = {**value, 'name': '  ' + value['name'].lower() + '  '}
    resumed = h.command('operator', 'department.create', variant, expected_fingerprint=token(h, variant))
    assert resumed['created'] is False
    assert resumed['department'] == created['department']
    assert resumed['first_admin_binding'] == created['first_admin_binding']
    department = created['department']
    assert department['generation_defaults'] == s.GenerationOptions().model_dump(mode='json')
    assert department['wish_settings'] == s.WishSettings().model_dump(mode='json')
    assert department['bulk_settings'] == s.BulkSettings().model_dump(mode='json')
    assert department['session_idle_minutes'] == s.SESSION_IDLE_MINUTES_DEFAULT
    assert department['session_max_hours'] == s.SESSION_MAX_HOURS_DEFAULT
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Organisation, organisation['id']).revision == organisation['revision'] + 1
        assert db.get(m.Department, department['id']).catalogue_revision > 1
        assert held_roles(db, operator, department['id']) == frozenset()
        assert current_roles(db, value['first_admin_identity_id'], department['id']) >= {'admin'}
        bindings = list(db.scalars(select(m.RoleBinding).where(m.RoleBinding.department_id == department['id'])))
        assert len(bindings) == 1 and bindings[0].identity_id == value['first_admin_identity_id']
        histories = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.department_id == department['id'])))
        assert {row.kind for row in histories} == {'department_settings', 'access'}
        assert all(row.actor_id == operator and row.reason == value['reason'] for row in histories)
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.department_id == department['id'])))
        assert {row.action for row in events} == {'department.create', 'role.grant'}
        assert all(row.actor_id == operator and row.reason == value['reason']
                   and row.detail['receipt_id'] == created['receipt_id'] for row in events)
        source = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.department_id == h.seed['department_id'],
            m.AuditEvent.action == 'department.create')))
        assert len(source) == 2 and all(row.target_id == department['id'] and row.reason == value['reason'] for row in source)
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        client.headers['origin'] = h.settings.public_origin
        response = client.post('/auth/login', json={'username': 'admin', 'password': TEST_PASSWORD,
                                                   'department_id': department['id']})
        assert response.status_code == 200, response.text
        me = client.get('/api/me').json()
        assert me['department']['id'] == department['id'] and 'admin' in me['roles']
        response = client.post('/auth/login', json={'username': 'operator', 'password': TEST_PASSWORD,
                                                   'department_id': department['id']})
        assert response.status_code == 401 and response.json()['error']['code'] == 'INVALID_LOGIN', response.text


def test_an_accepted_managed_account_can_be_the_first_administrator(department_h):
    h = department_h
    value = payload(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Account(identity_id=value['first_admin_identity_id'], username='accepted-admin',
                         email='accepted@example.test', accepted_at=m.now()))
    assert h.command('operator', 'department.create', value, expected_fingerprint=token(h, value))['created'] is True


def test_same_name_cannot_grant_a_different_administrator(department_h):
    h = department_h
    value = payload(h)
    created = h.command('operator', 'department.create', value, expected_fingerprint=token(h, value))
    other = h.get('coordinator', '/api/me')['identity_id']
    reply = h.command('operator', 'department.create', {**value, 'first_admin_identity_id': other}, status=409)
    assert reply['error']['code'] == 'FIRST_ADMIN_CONFLICT'
    with transaction(h.app.state.engine) as db:
        assert held_roles(db, other, created['department']['id']) == frozenset()


def test_case_and_space_variants_resolve_to_the_existing_department_without_renaming_it(department_h):
    h = department_h
    value = payload(h, name='  Golden Scenarios (test)  ')
    created = h.command('operator', 'department.create', value, expected_fingerprint=token(h, value))
    assert created['department']['name'] == value['name']
    variant = {**value, 'name': 'golden scenarios (test) '}
    resumed = h.command('operator', 'department.create', variant, expected_fingerprint=token(h, variant))
    assert resumed['created'] is False and resumed['department'] == created['department']
    assert resumed['first_admin_binding'] == created['first_admin_binding']


def test_case_variant_duplicate_departments_fail_closed(department_h):
    h = department_h
    value = payload(h)
    with transaction(h.app.state.engine, write=True) as db:
        for name in ('Golden Scenarios (test)', 'golden scenarios (test) '):
            db.add(m.Department(organisation_id=h.seed['organisation_id'], name=name))
    assert preview(h, value, status=409)['error']['code'] == 'DEPARTMENT_NAME_CONFLICT'
    reply = h.command('operator', 'department.create', value, status=409)
    assert reply['error']['code'] == 'DEPARTMENT_NAME_CONFLICT'


@pytest.mark.parametrize('new_first_admin', [False, True])
def test_preview_lists_org_settings_holders_lost_before_confirmation(department_h, new_first_admin):
    h = department_h
    value = payload(h)
    h.client('operator')
    admin, other = (h.seed['identities'][role] for role in ('admin', 'coordinator'))
    with transaction(h.app.state.engine, write=True) as db:
        second = m.Department(organisation_id=h.seed['organisation_id'], name='Second department (test)')
        db.add(second)
        db.flush()
        for who, department_id in ((admin, second.id), (other, second.id), (other, h.seed['department_id'])):
            db.add(m.RoleBinding(identity_id=who, department_id=department_id, role='admin', starts_on=date(2020, 1, 1)))
        if new_first_admin:
            identity = m.Identity(issuer='local-development', subject='new-admin', display_name='New administrator (test)',
                                  password_hash=db.get(m.Identity, admin).password_hash)
            db.add(identity)
            db.flush()
            db.add(m.Account(identity_id=identity.id, username='new-admin', email='new@example.test', accepted_at=m.now()))
            value['first_admin_identity_id'] = identity.id
    models = (m.Department, m.RoleBinding, m.DefinitionHistory, m.CommandReceipt, m.AuditEvent)
    with transaction(h.app.state.engine) as db:
        counts = [db.scalar(select(func.count()).select_from(model)) for model in models]
    review = preview(h, value)
    s.DepartmentCreatePreview.model_validate(review)
    lost = review['org_settings_holders_lost']
    assert {row['identity_id'] for row in lost} == ({admin, other} if new_first_admin else {other})
    assert all(row['display_name'].endswith('(test)') for row in lost)
    assert review['would_create'] is True and review['department_id'] is None
    with transaction(h.app.state.engine) as db:
        assert [db.scalar(select(func.count()).select_from(model)) for model in models] == counts
    created = h.command('operator', 'department.create', value, expected_fingerprint=review['expected_fingerprint'])
    assert created['created'] is True
    assert preview(h, value)['org_settings_holders_lost'] == []


def test_creation_requires_preview_token_bound_to_the_exact_change(department_h):
    h = department_h
    value = payload(h)
    review = preview(h, value)
    assert h.command('operator', 'department.create', value, status=428)['error']['code'] == 'PREVIEW_REQUIRED'
    changed = {**value, 'reason': 'A changed reason must be reviewed again'}
    reply = h.command('operator', 'department.create', changed, status=409,
                      expected_fingerprint=review['expected_fingerprint'])
    assert reply['error']['code'] == 'STALE_PREVIEW'
    assert h.command('operator', 'department.create', changed, expected_fingerprint=token(h, changed))['created'] is True


def test_preview_token_expires_when_another_holder_would_lose_authority(department_h):
    h = department_h
    value = payload(h)
    review = preview(h, value)
    h.command('admin', 'role.grant', {'identity_id': h.seed['identities']['coordinator'], 'role': 'admin',
        'starts_on': '2020-01-01', 'reason': 'Give another synthetic administrator organisation settings authority'})
    reply = h.command('operator', 'department.create', value, status=409,
                      expected_fingerprint=review['expected_fingerprint'])
    assert reply['error']['code'] == 'STALE_PREVIEW'
    renewed = preview(h, value)
    assert [row['identity_id'] for row in renewed['org_settings_holders_lost']] == [h.seed['identities']['coordinator']]
    assert renewed['expected_fingerprint'] != review['expected_fingerprint']


def test_failure_rolls_back_department_binding_history_and_receipt(department_h, monkeypatch):
    h = department_h
    value, key = payload(h), str(uuid4())
    fingerprint = token(h, value)
    original = departments.history_many

    def fail_after_history(*args, **kwargs):
        original(*args, **kwargs)
        raise Problem(409, 'TEST_INTERRUPTED', 'Synthetic interruption after the binding and history flush')

    with monkeypatch.context() as patch:
        patch.setattr(departments, 'history_many', fail_after_history)
        h.command('operator', 'department.create', value, key=key, status=409, expected_fingerprint=fingerprint)
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Department).where(m.Department.name == value['name'])) is None
        assert db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.key == key)) is None
        assert db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.reason == value['reason'])) is None
    assert h.command('operator', 'department.create', value, key=key, expected_fingerprint=fingerprint)['created'] is True


def test_production_also_refuses_replayed_and_recovered_commands(department_h):
    h = department_h
    value, key = payload(h), str(uuid4())
    fingerprint = token(h, value)
    h.command('operator', 'department.create', value, key=key, expected_fingerprint=fingerprint)
    object.__setattr__(h.settings, 'deployment', 'production')
    for response in (h.command('operator', 'department.create', value, key=key, status=403, expected_fingerprint=fingerprint),
                     h.get('operator', '/api/commands/' + key, status=403)):
        assert response['error']['code'] == 'TEST_DEPLOYMENT_REQUIRED'


def test_organisation_settings_still_require_admin_in_every_department(department_h):
    h = department_h
    value = payload(h, first_admin_identity_id=h.get('coordinator', '/api/me')['identity_id'])
    h.command('operator', 'department.create', value, expected_fingerprint=token(h, value))
    me = h.get('admin', '/api/me')
    department, organisation = me['department'], me['organisation']
    settings = {'department_name': department['name'], 'department_revision': department['revision'],
        'organisation_name': organisation['name'] + ' changed', 'timezone': organisation['timezone'],
        'week_start': organisation['week_start'], 'reason': 'Check organisation-wide authority after department creation'}
    reply = h.command('admin', 'department.settings', settings, department['id'], organisation['revision'], status=403)
    assert reply['error']['code'] == 'ORGANISATION_ADMIN_REQUIRED'
    h.command('admin', 'department.settings', {**settings, 'organisation_name': organisation['name']},
              department['id'], organisation['revision'])


@pytest.mark.postgres
def test_concurrent_same_name_requests_create_one_department_and_one_binding(pg):
    name_test_administrators(pg)
    value, client = payload(pg), pg.client('operator')
    fingerprint = token(pg, value)

    def create(_):
        return client.post('/api/commands', headers={'idempotency-key': str(uuid4())},
                           json={'kind': 'department.create', 'payload': value, 'expected_fingerprint': fingerprint})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(create, range(2)))
    assert all(response.status_code == 200 for response in responses), [r.text for r in responses]
    assert sorted(response.json()['created'] for response in responses) == [False, True]
    assert len({response.json()['department']['id'] for response in responses}) == 1
    with transaction(pg.app.state.engine) as db:
        department = db.scalar(select(m.Department).where(m.Department.name == value['name']))
        assert len(list(db.scalars(select(m.RoleBinding).where(m.RoleBinding.department_id == department.id)))) == 1


def test_a_new_department_starts_on_the_catalogue_rules_page(department_h):
    # Owner decision (19 Nov 2026): the catalogue Rules page is the default for new departments.
    h = department_h
    value = payload(h)
    created = h.command('operator', 'department.create', value, expected_fingerprint=token(h, value))
    assert created['department']['display_settings'] == {'rule_catalogue': True}
