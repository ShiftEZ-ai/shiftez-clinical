"""Budgets for the foreground API reads that share the authenticated department."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from apps.api import models as m
from apps.api.db import transaction


READS = {
    "/api/me": 17,
    "/api/home": 60,
    "/api/workspaces?limit=50": 30,
    "/api/requests/all?for_me=true": 55,
}


def populated_reads(h):
    h.workspace()
    start = datetime(2026, 10, 4, 6, tzinfo=timezone.utc)
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(m.RequestRecord(
            department_id=h.seed['department_id'], person_id=person, kind='meeting',
            start=start + timedelta(days=index), end=start + timedelta(days=index, hours=1),
            status='submitted', private_reason='Synthetic calendar request', created_by=h.seed['identities']['staff'],
        ) for index in range(5) for person in h.seed['people'][:10])
    return h.client('admin')


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_foreground_endpoint_select_budgets(request, backend):
    h = request.getfixturevalue(backend)
    client = populated_reads(h)
    selects = 0

    def count(_connection, _cursor, statement, _parameters, _context, _many):
        nonlocal selects
        if statement.lstrip().upper().startswith('SELECT'):
            selects += 1

    engine = h.app.state.engine
    observed = {}
    event.listen(engine, 'before_cursor_execute', count)
    try:
        for path in READS:
            selects = 0
            assert client.get(path).status_code == 200
            observed[path] = selects
    finally:
        event.remove(engine, 'before_cursor_execute', count)

    assert all(count <= READS[path] for path, count in observed.items()), observed
