"""Request-local reads respect concurrent revocation, writes and transaction boundaries."""
from datetime import timedelta

import pytest
from fastapi import Request
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import authenticate, held_roles
from apps.api.db import transaction
from apps.api.services.independence import active_permissions, today
from tests.governance_support import identity


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def backend(request):
    return request.getfixturevalue(request.param)


def test_revocation_is_visible_at_the_next_live_check_and_request(backend):
    h = backend
    client = h.client('staff')
    binding = next(b for b in h.get('admin', '/api/access')['bindings']
                   if b['identity_id'] == h.seed['identities']['staff'] and b['role'] == 'staff')
    assert client.get('/api/me').status_code == 200
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies['rota_session'], settings=h.settings)
        db.info['_request_parents'] = {}
        assert held_roles(db, actor.identity_id, actor.department_id) == {'staff'}
        h.command('admin', 'role.revoke', {'reason': 'Department access ended'}, binding['id'], binding['revision'])
        assert held_roles(db, actor.identity_id, actor.department_id) == set()
        assert actor.permits('staff')
    refused = client.get('/api/me')
    assert refused.status_code == 403
    assert refused.json()['error']['code'] == 'FORBIDDEN'


def test_self_approval_grant_and_expiry_are_visible_on_next_request(backend, monkeypatch):
    h = backend
    subject = identity(h, 'dual_role', ['coordinator', 'approver'])
    assert h.get('dual_role', '/api/me')['self_approval'] == []
    with transaction(h.app.state.engine) as db:
        day = today(db, h.seed['department_id'])
    ends = day + timedelta(days=1)
    h.command('admin', 'self_approval.grant', {
        'identity_id': subject, 'actions': ['rota.approve'],
        'starts_on': day.isoformat(), 'ends_on': ends.isoformat(), 'reason': 'Temporary independent cover gap',
    })
    assert h.get('dual_role', '/api/me')['self_approval'] == ['rota.approve']
    clock = m.now
    monkeypatch.setattr(m, 'now', lambda: clock() + timedelta(days=1))
    assert h.get('dual_role', '/api/me')['self_approval'] == []


def test_writer_reads_its_changes_and_discards_rolled_back_authority(backend):
    h = backend
    engine = h.app.state.engine
    subject = h.seed['identities']['staff']
    department_id, organisation_id = h.seed['department_id'], h.seed['organisation_id']

    @h.app.get('/api/test/request-authority')
    def change(request: Request):
        with transaction(engine, write=True) as db:
            authenticate(db, request.cookies['rota_session'], settings=h.settings)
            assert held_roles(db, subject, department_id) == {'staff'}
            assert not active_permissions(db, department_id)
            old_department = db.get(m.Department, department_id).name
            old_organisation = db.get(m.Organisation, organisation_id).name
            day = today(db, department_id)
            savepoint = db.begin_nested()
            db.get(m.Department, department_id).name = old_department + ' Updated'
            db.get(m.Organisation, organisation_id).name = old_organisation + ' Updated'
            binding = m.RoleBinding(department_id=department_id, identity_id=subject, role='approver', starts_on=day)
            grant = m.SelfApprovalPermission(department_id=department_id, identity_id=subject, starts_on=day,
                ends_on=day + timedelta(days=1), actions=['rota.approve'], reason='Synthetic approval test',
                created_by=h.seed['identities']['admin'])
            db.add_all([binding, grant])
            db.flush()
            assert db.get(m.Department, department_id).name == old_department + ' Updated'
            assert db.get(m.Organisation, organisation_id).name == old_organisation + ' Updated'
            assert held_roles(db, subject, department_id) == {'staff', 'approver'}
            assert [p.id for p in active_permissions(db, department_id)] == [grant.id]
            savepoint.rollback()
            assert db.get(m.Department, department_id).name == old_department
            assert db.get(m.Organisation, organisation_id).name == old_organisation
            assert held_roles(db, subject, department_id) == {'staff'}
            assert not active_permissions(db, department_id)
        return {'verified': True}

    assert h.get('admin', '/api/test/request-authority') == {'verified': True}


def test_reused_session_does_not_retain_authority_across_commit(backend):
    h = backend
    engine = h.app.state.engine
    subject, department_id = h.seed['identities']['staff'], h.seed['department_id']

    @h.app.get('/api/test/transaction-authority')
    def check(request: Request):
        with transaction(engine, write=True) as db:
            authenticate(db, request.cookies['rota_session'], settings=h.settings)
            assert held_roles(db, subject, department_id) == {'staff'}
            db.commit()
            with transaction(engine, write=True) as change:
                change.scalar(select(m.RoleBinding).where(
                    m.RoleBinding.identity_id == subject, m.RoleBinding.department_id == department_id,
                    m.RoleBinding.role == 'staff')).revoked = True
            assert not held_roles(db, subject, department_id)
        return {'verified': True}

    assert h.get('admin', '/api/test/transaction-authority') == {'verified': True}
