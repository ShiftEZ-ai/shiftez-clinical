from datetime import date
from decimal import Decimal

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.snapshots import shift_record, workspace_shifts


def test_projection_json_preserves_fastapi_wire_values():
    from apps.api.serialization import projection_json_value

    projection = {0: {'_sa_private': 1, 'values': [1, None, False, -0.0, 1e-07, '\u2028caf\u00e9']},
                  'nested': [{'date': date(2026, 10, 15), 'amount': Decimal('0.25')},
                             ('_sa_text', {'_sa_other': 2, 'value': 3})]}
    assert JSONResponse(projection_json_value(projection)).body == JSONResponse(jsonable_encoder(projection)).body


@pytest.mark.parametrize('fixture', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_workspace_shifts_batch_reads_without_constructing_assignments(request, fixture):
    h = request.getfixturevalue(fixture)
    workspace = h.shift(h.workspace(), people=[h.seed['people'][0]])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T12:00:00Z', end='2026-10-05T13:00:00Z',
                        people=[h.seed['people'][1]])['workspace']
    assignment_inits = []
    selects = []

    def constructed(target, args, kwargs):
        assignment_inits.append(1)

    def queried(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith('SELECT '):
            selects.append(1)

    engine = h.app.state.engine
    event.listen(m.Assignment, 'init', constructed)
    event.listen(engine, 'before_cursor_execute', queried)
    try:
        with transaction(engine) as db:
            shifts = workspace_shifts(db, workspace['id'])
    finally:
        event.remove(engine, 'before_cursor_execute', queried)
        event.remove(m.Assignment, 'init', constructed)

    assert sum(len(shift['participants']) for shift in shifts) == 2
    assert len(selects) <= 2
    assert assignment_inits == []

    with transaction(engine) as db:
        rows = db.scalars(select(m.Shift).where(m.Shift.workspace_id == workspace['id'])
                          .order_by(m.Shift.start, m.Shift.id))
        assert shifts == [shift_record(db, row) for row in rows]


def test_department_scale_shift_links_use_the_workspace_in_both_arms(scale_harness):
    from tests.test_backend_scale import scale_department

    h = scale_harness()
    workspace, *_ = scale_department(h)
    client = h.client('coordinator')
    counts = []

    def collect(_conn, _cursor, statement, _args, _context, _many):
        if 'FROM shift_services' in statement and 'UNION ALL' in statement:
            counts.append((statement.count('SELECT shifts.id'), statement.count('shifts.workspace_id = ')))

    engine = h.app.state.engine
    event.listen(engine, 'before_cursor_execute', collect)
    try:
        response = client.get('/api/workspaces/' + workspace['id'], headers={'accept-encoding': 'identity'})
    finally:
        event.remove(engine, 'before_cursor_execute', collect)

    assert response.status_code == 200
    shifts = response.json()['shifts']
    assert len(shifts) >= 500
    assert all(shift['service_ids'] and shift['participants'] for shift in shifts)
    assert counts == [(2, 2)]
