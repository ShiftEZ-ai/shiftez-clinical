"""Department writers stay exclusive without blocking independent session audit inserts."""
import json
import time
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from apps.api import models as m
from apps.api.auth import digest
from apps.api.db import transaction
from apps.api.services import leadership, table_views


@pytest.mark.postgres
@pytest.mark.parametrize('writer', ['saved_views', 'leadership'])
def test_department_writer_allows_atomic_logout_while_held(pg, writer):
    client = pg.client('staff')
    token_hash = digest(client.cookies['rota_session'])
    actor = SimpleNamespace(department_id=pg.seed['department_id'])
    engine = pg.app.state.engine
    with transaction(engine, write=True) as holder:
        if writer == 'saved_views':
            table_views.lock_department_views(holder, actor)
        else:
            leadership.conversion_plan(holder, actor, date(2026, 11, 1), lock=True)
        # Other writers and key-changing operations must still be excluded.
        for key_share in (True, False):
            with engine.begin() as contender:
                with pytest.raises(OperationalError) as caught:
                    contender.execute(select(m.Department.id).where(m.Department.id == actor.department_id)
                                      .with_for_update(key_share=key_share, nowait=True))
                assert caught.value.orig.sqlstate == '55P03'
        started = time.monotonic()
        response = client.post('/auth/logout')
        elapsed = time.monotonic() - started
        print(json.dumps({'writer': writer, 'status': response.status_code, 'logout_seconds': elapsed}))
        assert response.status_code == 200, response.text
        assert 'rota_session' not in client.cookies and 'rota_csrf' not in client.cookies
        assert client.get('/api/me').status_code == 401
        with transaction(engine) as check:
            session = check.scalar(select(m.Session).where(m.Session.token_hash == token_hash))
            assert session.revoked
            assert check.scalar(select(m.AuditEvent.id).where(
                m.AuditEvent.action == 'auth.sign_out', m.AuditEvent.target_id == session.id)) is not None


@pytest.mark.postgres
def test_incompatible_department_lock_rolls_back_logout_and_audit_together(pg):
    client = pg.client('staff')
    token_hash = digest(client.cookies['rota_session'])
    engine = pg.app.state.engine
    with engine.begin() as holder:
        holder.execute(select(m.Department.id).where(m.Department.id == pg.seed['department_id']).with_for_update())
        response = client.post('/auth/logout')
        assert response.status_code == 503, response.text
        with transaction(engine) as check:
            session = check.scalar(select(m.Session).where(m.Session.token_hash == token_hash))
            assert not session.revoked
            assert check.scalar(select(m.AuditEvent.id).where(
                m.AuditEvent.action == 'auth.sign_out', m.AuditEvent.target_id == session.id)) is None
