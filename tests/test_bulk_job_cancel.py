"""Stopping a set of rule checks uses one atomic command and the ordinary job guards."""
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk import preview


def jobs(h, statuses):
    workspace = h.workspace()
    with transaction(h.app.state.engine, write=True) as db:
        rows = [m.Job(department_id=h.seed['department_id'], workspace_id=workspace['id'],
            requested_by=h.seed['identities']['coordinator'], kind='feasibility', status=status,
            input_fingerprint='frozen', input_snapshot={}) for status in statuses]
        db.add_all(rows)
        db.flush()
        return [row.id for row in rows]


def states(h, ids):
    with transaction(h.app.state.engine) as db:
        return dict(db.execute(select(m.Job.id, m.Job.status).where(m.Job.id.in_(ids))).all())


def body(ids):
    return {'item_kind': 'job.cancel', 'items': [{'target_id': ident} for ident in ids],
            'reason': 'Stop the selected rule checks'}


def test_bulk_job_cancel_previews_without_writes_and_commits_once(h):
    ids = jobs(h, ['queued', 'running'])
    before = states(h, ids)
    checked = preview(h, 'coordinator', body(ids))
    assert checked['counts']['ready'] == 2 and not checked['undoable'], checked
    assert states(h, ids) == before
    key = str(uuid.uuid4())
    result = h.command('coordinator', 'bulk.apply', body(ids), key=key)
    assert result['counts']['changed'] == 2 and not result['undoable']
    assert states(h, ids) == dict.fromkeys(ids, 'cancelled')
    assert h.command('coordinator', 'bulk.apply', body(ids), key=key)['receipt_id'] == result['receipt_id']
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'job.cancel')))
        assert {event.target_id for event in events} == set(ids)
        assert all(event.detail['batch_id'] == result['batch_id'] for event in events)


def test_bulk_job_cancel_rolls_back_if_a_check_has_finished(h):
    ids = jobs(h, ['queued', 'succeeded'])
    before = states(h, ids)
    error = h.command('coordinator', 'bulk.apply', body(ids), status=409)['error']
    assert error['code'] == 'JOB_STATE' and error['details']['failed_index'] == 1
    assert states(h, ids) == before


@pytest.mark.parametrize('role', ['staff', 'approver', 'credentialer'])
def test_bulk_job_cancel_keeps_single_job_permissions(h, role):
    ids = jobs(h, ['queued'])
    before = states(h, ids)
    assert h.command(role, 'bulk.apply', body(ids), status=403)['error']['code'] == 'FORBIDDEN'
    assert states(h, ids) == before
