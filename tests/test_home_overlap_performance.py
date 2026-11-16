"""Home's draft overlap work must not grow with persisted publication history."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event, insert

from apps.api import models as m
from apps.api.db import transaction


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_home_overlap_ignores_persisted_history(request, backend):
    h = request.getfixturevalue(backend)
    workspace = h.shift(h.workspace('Live draft'), start='2026-10-06T08:00:00+03:00',
                        end='2026-10-06T12:00:00+03:00')['workspace']
    person = h.seed['people'][0]
    start = datetime(2026, 10, 6, 5, tzinfo=timezone.utc)
    with transaction(h.app.state.engine, write=True) as db:
        history = m.Workspace(department_id=h.seed['department_id'], name='Retained history',
            starts_on=date(2025, 10, 1), ends_on=date(2025, 11, 1), status='draft',
            created_by=h.seed['identities']['coordinator'], last_edited_by=h.seed['identities']['coordinator'])
        db.add(history)
        db.flush()
        shifts = [dict(id=m.uid(), workspace_id=history.id, assignment_type_id=h.seed['type'],
            type_revision=1, type_snapshot={}, location_id=h.seed['locations'][0],
            start=start-timedelta(days=365), end=start-timedelta(days=365)+timedelta(hours=4)) for _ in range(1000)]
        db.execute(insert(m.Shift), shifts)
        db.execute(insert(m.Assignment), [dict(shift_id=shift['id'], person_id=person,
            start=shift['start'], end=shift['end']) for shift in shifts])
        history.status = 'published'
        db.execute(insert(m.RequestRecord), [dict(department_id=h.seed['department_id'], person_id=person,
            kind='leave', start=start, end=start+timedelta(hours=4), status='approved',
            private_reason='Synthetic performance fixture', created_by=h.seed['identities']['staff']) for _ in range(200)])
    h.client('coordinator')
    steps, queries = [], []
    active = [0]

    def progress():
        active[0] += 1
        return 0

    def before(conn, cursor, statement, parameters, context, many):
        if 'FROM assignments JOIN shifts' in statement and 'JOIN requests' in statement:
            queries.append(statement)
            active[0] = 0
            if backend == 'h':
                conn.connection.driver_connection.set_progress_handler(progress, 100)

    def after(conn, cursor, statement, parameters, context, many):
        if 'FROM assignments JOIN shifts' in statement and 'JOIN requests' in statement and backend == 'h':
            conn.connection.driver_connection.set_progress_handler(None, 0)
            steps.append(active[0] * 100)

    engine = h.app.state.engine
    event.listen(engine, 'before_cursor_execute', before)
    event.listen(engine, 'after_cursor_execute', after)
    try:
        result = h.get('coordinator', '/api/home')
    finally:
        event.remove(engine, 'before_cursor_execute', before)
        event.remove(engine, 'after_cursor_execute', after)
    conflict = next(item for item in result['attention'] if item['key'] == 'leave_conflicts')
    assert conflict['count'] == 1 and conflict['sample'][0]['rota_id'] == workspace['id']
    assert queries and all('shifts.workspace_id IN (' in query for query in queries)
    if backend == 'h':
        # VM instructions are independent of host load and Python tracing/CI CPU multipliers.
        assert max(steps) < 50_000, steps
