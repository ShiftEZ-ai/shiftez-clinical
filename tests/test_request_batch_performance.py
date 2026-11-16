"""Approval reads stay bounded while each request retains its own authority checks."""
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from tests.test_backend_scale import scale_department
from tests.test_request_decide_many import decide


@pytest.fixture(scope='module', params=[50, 150])
def approval_measurement(scale_harness, request):
    h = scale_harness()
    scale_department(h, assigned=False)
    count = request.param
    with transaction(h.app.state.engine, write=True) as db:
        reviewer = db.get(m.Identity, h.seed['identities']['approver']).person_id
        people = list(db.scalars(select(m.Person.id).where(m.Person.organisation_id == h.seed['organisation_id'])))
        people = [person for person in people if person != reviewer][:count]
        assert len(people) == count
        at = datetime(2026, 10, 4, 6, tzinfo=timezone.utc)
        rows = [m.RequestRecord(department_id=h.seed['department_id'], person_id=person,
            kind='meeting', start=at, end=at + timedelta(hours=1), private_reason='Synthetic scale request',
            created_by=h.seed['identities']['coordinator']) for person in people]
        db.add_all(rows)
        db.flush()
        items = [{'id': row.id, 'revision': row.revision, 'source': 'request'} for row in rows]
    h.client('approver')
    reads = Counter()

    def queried(conn, cursor, statement, parameters, context, executemany):
        text = ' '.join(statement.lower().split())
        if text.startswith('select'):
            reads['total'] += 1
            for table in ('departments', 'organisations', 'identities', 'role_bindings', 'self_approval_permissions'):
                if ' from ' + table + ' ' in text:
                    reads[table] += 1

    event.listen(h.app.state.engine, 'before_cursor_execute', queried)
    try:
        result = decide(h, items)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', queried)
    return count, result, reads


def test_department_approval_reads_do_not_grow_per_request(approval_measurement):
    count, result, reads = approval_measurement
    assert result['count'] == count
    assert all(row['status'] == 'approved' and row['revision'] == 2 for row in result['records'])
    assert reads['total'] <= 45, reads
    assert all(reads[table] <= 4 for table in
               ('departments', 'organisations', 'identities', 'role_bindings', 'self_approval_permissions')), reads
