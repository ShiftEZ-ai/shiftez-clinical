import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.snapshots import shift_record, workspace_shifts


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
