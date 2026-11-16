"""A failed activity bump cannot replace the foreground request's own result."""
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from apps.api import auth, models as m
from apps.api import main
from apps.api.db import DatabaseWriteTimeout, transaction
from tests.test_fix_list_performance import backend


@pytest.mark.parametrize('failure', [DatabaseWriteTimeout(),
    OperationalError('synthetic liveness write', {}, RuntimeError('synthetic lock failure')),
    RuntimeError('synthetic liveness failure')], ids=['timeout', 'operational', 'unexpected'])
def test_failed_keep_alive_is_logged_once_and_command_still_runs(backend, monkeypatch, caplog, failure):
    h = backend
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        session = db.scalar(select(m.Session).where(
            m.Session.token_hash == auth.digest(client.cookies['rota_session'])))
        session.last_active_at = m.now() - timedelta(minutes=2)
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(auth, 'update', fail)
    response = client.post('/api/commands', headers={'idempotency-key': m.uid()}, json={
        'kind': 'workspace.create', 'payload': {'name': 'Best effort activity',
            'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}})
    assert response.status_code == 200, response.text
    skipped = [record for record in caplog.records if record.name == 'rota.auth' and
               record.message == 'session_liveness_write_skipped']
    assert len(skipped) == 1 and skipped[0].levelname == 'WARNING'


def test_foreground_guard_skips_a_direct_keep_alive_timeout(h, monkeypatch, caplog):
    client = h.client('coordinator')
    def fail(*args, **kwargs):
        raise DatabaseWriteTimeout()
    monkeypatch.setattr(main, 'keep_alive', fail)
    response = client.post('/api/commands', headers={'idempotency-key': m.uid()}, json={
        'kind': 'workspace.create', 'payload': {'name': 'Best effort request guard',
            'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}})
    assert response.status_code == 200, response.text
    skipped = [record for record in caplog.records if record.message == 'session_liveness_write_skipped']
    assert len(skipped) == 1 and skipped[0].levelname == 'WARNING'
