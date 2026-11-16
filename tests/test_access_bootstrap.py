"""Missing Developer access requires the existing audited owner-maintenance path."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from scripts.system_command import apply, maintenance_identity


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_admin_cannot_self_grant_but_owner_can_recover_developer_access(request, backend):
    h = request.getfixturevalue(backend)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['operator']).disabled = True
    payload = {'identity_id': h.seed['identities']['admin'], 'role': 'operator', 'starts_on': '2020-01-01',
               'reason': 'Owner recovery of missing Developer access'}
    denied = h.command('admin', 'role.grant', payload, status=403)
    assert denied['error']['code'] == 'SYSTEM_ROLE_GRANT'
    with transaction(h.app.state.engine, write=True) as db:
        assert db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == payload['identity_id'],
            m.RoleBinding.role == 'operator')) is None
        owner = maintenance_identity(db, m, select)
        department = db.get(m.Department, h.seed['department_id'])
        result = apply(db, owner, department, [{'kind': 'role.grant', 'payload': payload}],
                       'synthetic-owner-recovery', h.settings)[0]
    assert result['record']['role'] == 'operator'
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.AuditEvent).where(m.AuditEvent.correlation_id == 'synthetic-owner-recovery'))
        assert event.action == 'role.grant' and event.detail['maintenance'] is True
        assert db.get(m.CommandReceipt, result['receipt_id']) is not None
    granted = h.command('admin', 'role.grant', {**payload, 'identity_id': h.seed['identities']['staff']})
    assert granted['record']['role'] == 'operator'
