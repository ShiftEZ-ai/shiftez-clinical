"""Final publication inputs are acquired after flush under the serialized writer."""
from contextlib import closing
from datetime import datetime
import sqlite3

import pytest
from sqlalchemy import inspect, select, text

from apps.api import models as m, snapshots
from apps.api.db import WRITE_LOCK_KEY, transaction
from apps.api.services import planning
from tests.test_fix_list_performance import backend
from tests.test_performance_budget import python_budget


def _other_writer_is_blocked(h, db):
    assert db.connection().info['rota_write']['serializing'] is False
    if h.app.state.engine.dialect.name == 'postgresql':
        with h.app.state.engine.connect() as other:
            assert other.execute(text('SELECT pg_try_advisory_xact_lock(:key)'),
                                 {'key': WRITE_LOCK_KEY}).scalar_one() is False
    else:
        with closing(sqlite3.connect(h.app.state.engine.url.database, timeout=0)) as other:
            with pytest.raises(sqlite3.OperationalError) as blocked:
                other.execute('BEGIN IMMEDIATE')
            assert blocked.value.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def test_rule_activated_after_preliminary_read_refuses_publication(backend, monkeypatch):
    h = backend
    typ = h.configure('assignment_types', {'name': 'Synthetic next-day duty',
        'starts_on': '2020-01-01', 'definition': {}})['record']
    workspace = h.shift(h.workspace())['workspace']
    workspace = h.shift(workspace, '2026-10-05T08:00:00+03:00',
                        '2026-10-05T12:00:00+03:00', typ=typ['id'])['workspace']
    workspace = h.lifecycle(workspace, 'submit')['workspace']
    workspace = h.lifecycle(workspace, 'approve', 'approver')['workspace']
    rule = h.command('coordinator', 'policy.create', {
        'name': 'Synthetic late transition rule', 'owner': 'Synthetic owner', 'source': 'Regression',
        'starts_on': '2020-01-01', 'definition': {
            'rule': {'kind': 'forbidden_transition', 'from_type_ids': [h.seed['type']],
                     'to_type_ids': [typ['id']]},
            'scope': {'person_id': h.seed['people'][0]}, 'severity': 'mandatory',
            'exception_allowed': False}})['record']
    activating, committed, acquired, writer = False, False, [], []
    original = planning.domain_snapshot

    def snapshot(db, row, *args, **kwargs):
        nonlocal activating, committed
        data = original(db, row, *args, **kwargs)
        if row.id == workspace['id'] and not activating:
            locked = bool(db.connection().info.get('rota_write'))
            acquired.append((locked, data['fingerprint']))
            if locked:
                assert committed
                writer.append(db)
            else:
                assert not committed
                activating = True
                try:
                    # A separate command/session commits while the preliminary reader stays open.
                    h.command('approver', 'policy.activate', {'reason': 'Activate in the admission window'},
                              rule['id'], rule['revision'])
                    committed = True
                finally:
                    activating = False
        return data

    evaluate = planning.evaluate

    def checked(data, **kwargs):
        if not activating:
            assert committed and len(writer) == 1
            # No cooperating activation can commit between this snapshot and its use.
            _other_writer_is_blocked(h, writer[0])
        return evaluate(data, **kwargs)

    monkeypatch.setattr(planning, 'domain_snapshot', snapshot)
    monkeypatch.setattr(planning, 'evaluate', checked)
    refused = h.lifecycle(workspace, 'publish', status=422)['error']
    assert refused['code'] == 'VALIDATION_FAILED'
    assert refused['details']['stale_approval']['code'] == 'STALE_APPROVAL'
    assert 'FORBIDDEN_TRANSITION' in {f['code'] for f in refused['details']['findings']}
    assert [locked for locked, _ in acquired] == [False, True]
    assert acquired[0][1] != acquired[1][1]
    assert h.detail(workspace)['workspace']['status'] == 'approved'
    assert not h.get('coordinator', '/api/publications')['publications']


def test_pending_writer_input_is_flushed_before_publish_snapshot(backend, monkeypatch):
    h = backend
    workspace = h.lifecycle(h.shift(h.workspace())['workspace'], 'submit')['workspace']
    workspace = h.lifecycle(workspace, 'approve', 'approver')['workspace']
    pending, acquired = [], []
    revalidate, snapshot = planning.revalidate, snapshots._domain_snapshot

    def pending_input(db, *args, **kwargs):
        assert db.connection().info.get('rota_write')
        row = m.Availability(person_id=h.seed['people'][0], department_id=h.seed['department_id'],
            kind='actual_absence', private_reason='Synthetic pending absence',
            recorded_by=h.seed['identities']['coordinator'],
            start=datetime.fromisoformat('2026-10-04T08:00:00+03:00'),
            end=datetime.fromisoformat('2026-10-04T12:00:00+03:00'))
        db.add(row)
        pending.append(row)
        return revalidate(db, *args, **kwargs)

    def acquired_after_flush(db, *args, **kwargs):
        if db.connection().info.get('rota_write'):
            assert len(pending) == 1 and inspect(pending[0]).persistent
            _other_writer_is_blocked(h, db)
            acquired.append(True)
        return snapshot(db, *args, **kwargs)

    monkeypatch.setattr(planning, 'revalidate', pending_input)
    monkeypatch.setattr(snapshots, '_domain_snapshot', acquired_after_flush)
    refused = h.lifecycle(workspace, 'publish', status=422)['error']
    assert refused['code'] == 'VALIDATION_FAILED'
    assert 'UNAVAILABLE' in {f['code'] for f in refused['details']['findings']}
    assert acquired == [True]
    assert h.detail(workspace)['workspace']['status'] == 'approved'
    assert not h.get('coordinator', '/api/publications')['publications']
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Availability.id).where(m.Availability.id == pending[0].id)) is None


@pytest.mark.cpu_budget
def test_department_publish_save_budget(backend, record_property):
    from tests.test_backend_scale import measure, post, scale_department

    h = backend
    workspace, data, assignments, _ = scale_department(h)
    workspace = h.lifecycle(workspace, 'submit')['workspace']
    workspace = h.lifecycle(workspace, 'approve', 'approver')['workspace']
    published, metrics = measure(h, 'publish', lambda: post(h, 'workspace.publish',
        {'reason': 'Department-scale publication measurement'}, workspace['id'], workspace['revision']))
    assert published['publication_id']
    assert (len(data['people']), len(data['focus_ids']), len(assignments)) == (163, 768, 2304)
    record_property('publish_wall_seconds', metrics['wall_seconds'])
    record_property('publish_cpu_seconds', metrics['cpu_seconds'])
    record_property('publish_save_budget_seconds', 5)
    # The shared tracing allowance keeps the native 5 s ceiling in the quiet budget lane.
    assert metrics['wall_seconds'] <= python_budget(5), metrics
