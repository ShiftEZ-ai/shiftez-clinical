"""Result retries never reload department-sized frozen input or prior evidence."""
import json
import re
import pytest

from sqlalchemy import event

from apps.api import models as m
from apps.api.db import transaction, retry_write
from apps.worker.main import finish_job
from tests.test_backend_scale import measure
from tests.test_performance_budget import python_budget

pytestmark = pytest.mark.postgres


def test_department_feasibility_finish_has_bounded_reads(pg, request):
    workspace = pg.workspace()
    # PC13 retained 53,727 candidates per phase, 34 MB of input and 60 MB of output.
    rows = [{'person_id': f'{i % 163:036d}', 'shift_id': f'{i // 163:036d}',
             'start': '2027-06-01T08:00:00+00:00', 'end': '2027-06-01T12:00:00+00:00',
             'eligible_and_available': False,
             'boundary': 'Other assignments, rest, workload and supervision are not checked here.',
             'exclusions': [{'code': 'RESTRICTION', 'rule_id': f'{(i + k) % 1000:036d}', 'rule_revision': 1}
                            for k in range(3)]} for i in range(53727)]
    check = {'status': 'requirements_conflict', 'candidate_evidence': rows, 'diagnostics': []}
    result = {'status': 'requirements_conflict', 'checks': {'baseline': check, 'proposed': check}}
    with transaction(pg.app.state.engine, write=True) as db:
        job = m.Job(department_id=pg.seed['department_id'], workspace_id=workspace['id'],
            requested_by=pg.seed['identities']['coordinator'], kind='feasibility', status='running',
            attempts=1, input_fingerprint='frozen', input_snapshot={'frozen': rows})
        db.add(job)
        db.flush()
        claim = {'id': job.id, 'attempt': 1}
    statements = []
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(pg.app.state.engine, 'before_cursor_execute', capture)
    try:
        accepted, timing = measure(pg, 'feasibility-finish', lambda: retry_write(pg.app.state.engine,
            lambda db: finish_job(db, claim, result, None)))
    finally:
        event.remove(pg.app.state.engine, 'before_cursor_execute', capture)
    request.node.user_properties.append(('feasibility_finish', json.dumps(timing)))
    assert accepted
    with transaction(pg.app.state.engine) as db:
        stored = db.get(m.Job, claim['id'])
        assert stored.status == 'succeeded' and stored.result == result
        assert stored.input_snapshot == {'frozen': rows}
    job_reads = [sql for sql in statements if sql.lstrip().startswith('SELECT') and 'FROM jobs' in sql]
    assert job_reads and not any(re.search(r'jobs\.(?:input_snapshot|result)\b', sql) for sql in job_reads)
    assert sum(timing['sql'].values()) <= 15, timing
    return locals()


@pytest.mark.wall_benchmark
def test_department_feasibility_finish_wall_budget(pg, request):
    state = test_department_feasibility_finish_has_bounded_reads(pg, request)
    assert state['timing']['wall_seconds'] < python_budget(5), state['timing']
