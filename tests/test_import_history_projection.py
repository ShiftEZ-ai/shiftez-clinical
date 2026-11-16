"""History lists return metadata without hydrating retained spreadsheet evidence."""
from datetime import datetime, timedelta, timezone
import time

import pytest
from sqlalchemy import event, inspect, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('staff', [False, True], ids=['sessions', 'staff'])
def test_import_history_projects_payloads_without_changing_metadata(request, backend, staff):
    h = request.getfixturevalue(backend)
    cls = m.StaffImportBatch if staff else m.ImportBatch
    excluded = ('rows', 'applied') if staff else ('rows',)
    workspace = None if staff else h.workspace()['id']
    endpoint = '/api/staff-imports' if staff else '/api/imports'
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Other synthetic department')
        db.add(other)
        db.flush()
        for n in range(104):
            values = dict(department_id=other.id if n == 103 else h.seed['department_id'],
                          source_name=f'Synthetic history {n}', source_hash=f'{n:064x}',
                          rows=[{'display_name': 'Synthetic person', 'note': 'x' * 256}] * 500 if 95 <= n < 103 else [],
                          status='preview', created_by=h.seed['identities']['coordinator'],
                          created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=n))
            if staff:
                values['applied'] = {'people': ['synthetic'] * 500} if n >= 95 else {}
            else:
                values.update(workspace_id=workspace, workspace_revision=1)
            db.add(cls(**values))
    with transaction(h.app.state.engine) as db:
        rows = list(db.scalars(select(cls).where(cls.department_id == h.seed['department_id'])
                               .order_by(cls.created_at.desc()).limit(100)))
        expected = [record(row, exclude=excluded) | ({'rows': len(row.rows)} if staff else {}) for row in rows]
    roles = ('admin', 'coordinator', 'credentialer') if staff else ('coordinator',)
    for role in roles:
        h.client(role)
    loaded = []

    def on_load(row, _context):
        loaded.append(inspect(row).unloaded)

    event.listen(cls, 'load', on_load)
    started, cpu = time.perf_counter(), time.process_time()
    try:
        actual = h.get(roles[0], endpoint)
    finally:
        elapsed, cpu_elapsed = time.perf_counter() - started, time.process_time() - cpu
        event.remove(cls, 'load', on_load)
    print(f'Import history {backend}/{cls.__tablename__}: {elapsed:.6f}s wall, {cpu_elapsed:.6f}s CPU, '
          f'{sum(not set(excluded).issubset(fields) for fields in loaded)} hydrated payload records')
    assert actual == {'batches': expected}
    assert len(actual['batches']) == 100
    assert loaded and all(set(excluded).issubset(fields) for fields in loaded)
    for role in roles[1:]:
        assert h.get(role, endpoint) == actual
    for role in ('staff', 'auditor'):
        h.get(role, endpoint, status=403)
