"""Job history keeps metadata reads independent of the stored scheduling input size."""
import gc
import re
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import event, insert, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import job_record
from packages.domain.solver_rules import COMPILER_VERSION
from tests.scheduler_departments import department
from tests.test_performance_budget import python_budget


def test_result_stale_classifies_obsolete_version_without_current_fingerprint():
    from apps.api.services.job_reads import result_stale

    job = SimpleNamespace(input_fingerprint='frozen')
    current = {'input_snapshot': {'generation_engine': 'cp_sat', 'compiler_version': COMPILER_VERSION},
               'result': {'algorithm_version': COMPILER_VERSION}}
    obsolete = {'input_snapshot': {'generation_engine': 'cp_sat', 'compiler_version': 'obsolete'},
                'result': {'algorithm_version': COMPILER_VERSION}}
    deferred = {'input_snapshot': {'generation_engine': 'cp_sat', 'compiler_version': 'obsolete',
                                   '_deferred_generation': True},
                'result': {'algorithm_version': COMPILER_VERSION}}

    assert result_stale(job, obsolete, None, None, input_fingerprint='frozen') is True
    assert result_stale(job, current, None, None, input_fingerprint='frozen') is None
    assert result_stale(job, deferred, None, None, input_fingerprint='frozen') is None
    assert result_stale(job, current, 'changed', None, input_fingerprint='frozen') is True


@pytest.mark.parametrize('snapshot,result,recheckable', [
    ({'generation_engine': 'cp_sat', 'compiler_version': COMPILER_VERSION},
     {'algorithm_version': COMPILER_VERSION}, True),
    ({'generation_engine': 'cp_sat', 'compiler_version': 'obsolete'}, {}, False),
    ({'generation_engine': 'cp_sat', 'compiler_version': COMPILER_VERSION},
     {'algorithm_version': 'obsolete'}, False),
    ({'planning_group_id': 'group-without-retained-population'}, {}, False),
    ({'generation_scope': 'selected', 'generation_scope_source': 'current'},
     {'generation_scope': 'selected', 'generation_scope_source': 'current'}, True),
    ({'generation_scope': 'selected', 'generation_scope_source': 'obsolete'},
     {'generation_scope': 'selected', 'generation_scope_source': 'obsolete'}, False),
])
def test_history_recheckability_uses_retained_scheduler_evidence(snapshot, result, recheckable):
    from apps.api.services.jobs import application_recheckable
    assert application_recheckable(snapshot, result, 'current') is recheckable


def job_history(h, jobs=12):
    data, _ = department(h, 29, people=163, days=28, rules='none', leave_rate=0, clinics=14)
    data['shifts'] = data['shifts'][:768]
    people = list(data['people'])
    additions = []
    for index, shift in enumerate(data['shifts']):
        shift['participants'] = [{
            'id': f'proposal-{index}-{slot}', 'person_id': people[(index * 3 + slot) % len(people)],
            'start': shift['start'], 'end': shift['end'], 'participation': 'full',
            'overtime': False, 'team_lead': slot == 0,
        } for slot in range(3)]
        additions.extend({'shift_id': shift['id'], 'participant': person} for person in shift['participants'])
    data.update(generation_engine='cp_sat', generation_options={'mode': 'fill_gaps'}, compiler_version='scale-test',
                diagnostic_proposal=False)
    rows = []
    for index in range(jobs):
        kind, snapshot = 'generation', data
        if index < 3:
            snapshot = {key: value for key, value in data.items() if key not in (
                'generation_engine', 'generation_options', 'compiler_version', 'diagnostic_proposal')}
            kind = ('generation', 'validation', 'feasibility')[index]
            if kind == 'feasibility':
                snapshot = {**snapshot, 'bindings': [{'policy_id': 'synthetic-policy', 'temporary_change': {'maximum': 5}}]}
        elif index == 3:
            snapshot = {**data, 'generation_engine': None, 'generation_options': None, 'compiler_version': None,
                        'diagnostic_proposal': None}
        rows.append({
            'id': f'scale-history-{index:02}', 'department_id': h.seed['department_id'],
            'workspace_id': data['workspace']['id'], 'requested_by': h.seed['identities']['coordinator'],
            'kind': kind, 'status': 'succeeded', 'input_fingerprint': data['fingerprint'],
            'input_snapshot': snapshot, 'result': {'status': 'candidate_feasible', 'additions': additions, 'removals': []},
        })
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Job), rows)
    assert len(data['people']) == 163 and len(data['shifts']) == 768 and len(additions) == 2304
    return data


def job_history_measurement(h):
    data = job_history(h)
    h.client('coordinator')
    gc.collect()
    reference_started, reference_cpu = time.perf_counter(), time.process_time()
    with transaction(h.app.state.engine) as db:
        expected = []
        seen = set()
        for row in db.scalars(select(m.Job).where(m.Job.department_id == h.seed['department_id'])
                              .order_by(m.Job.created_at.desc(), m.Job.seq.desc()).limit(100)):
            value = job_record(row, include_result=False)
            if row.kind == 'generation':
                # The synthetic compiler is obsolete and cannot be reconciled by Apply;
                # historical fixtures without retained metadata remain unknown.
                stale = True if row.input_snapshot.get('generation_engine') == 'cp_sat' else None
                if stale:
                    assert row.input_snapshot['compiler_version'] != COMPILER_VERSION
                # The newest generation is unknown; older same-workspace runs are fail-closed superseded.
                value.update(result_stale=stale,
                             currency='superseded' if row.workspace_id in seen else 'unknown')
                seen.add(row.workspace_id)
            expected.append(value)
    print(f'Job history full-input reference: {time.perf_counter() - reference_started:.3f}s wall / '
          f'{time.process_time() - reference_cpu:.3f}s CPU')
    statements = []

    def sql(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    # Keep collection of previous fixtures/full-snapshot reference reads outside this request's timing.
    gc.collect()
    event.listen(h.app.state.engine, 'before_cursor_execute', sql)
    began, cpu = time.perf_counter(), time.process_time()
    try:
        response = h.get('coordinator', '/api/jobs?workspace_id=' + data['workspace']['id'])
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', sql)
    elapsed, cpu_elapsed = time.perf_counter() - began, time.process_time() - cpu
    print(f'Job history: {elapsed:.3f}s wall / {cpu_elapsed:.3f}s CPU / {len(statements)} SQL statements '
          '/ 12 runs at 163 people, 768 sessions, 2304 assignments')
    return response, expected, statements, elapsed, cpu_elapsed


@pytest.fixture(scope='module')
def job_history_scale(scale_harness):
    return job_history_measurement(scale_harness('h'))


@pytest.fixture(scope='module')
def job_history_postgres_scale(scale_harness):
    return job_history_measurement(scale_harness('pg'))


def assert_job_history(result):
    response, expected, statements, _, _ = result
    assert response['jobs'] == expected
    job_reads = [sql for sql in statements if 'FROM jobs' in sql]
    assert len(job_reads) <= 2  # measured 2 on the 200 path: one stamp and one page statement
    # No JSON paths into retained megabyte inputs/results either: PostgreSQL re-parses JSON per extraction.
    assert not any(re.search(r'jobs\.(?:input_snapshot|result)\b', sql) for sql in job_reads)  # the stamp AND the page


def test_job_history_does_not_read_full_scheduling_inputs(job_history_scale):
    assert_job_history(job_history_scale)


@pytest.mark.cpu_budget
def test_job_history_does_not_read_full_scheduling_inputs_cpu_budget(job_history_scale):
    _, expected, _, _, cpu_elapsed = job_history_scale
    # Bound client decoding separately from shared database/scheduling delays.
    budget = python_budget(len(expected) / 12)
    assert cpu_elapsed < budget


@pytest.mark.wall_benchmark
def test_job_history_does_not_read_full_scheduling_inputs_wall_benchmark(job_history_scale):
    _, expected, _, elapsed, _ = job_history_scale
    budget = python_budget(len(expected) / 12)
    assert elapsed < budget * 4


@pytest.mark.postgres
def test_job_history_postgres(job_history_postgres_scale):
    assert_job_history(job_history_postgres_scale)


@pytest.mark.postgres
@pytest.mark.cpu_budget
def test_job_history_postgres_cpu_budget(job_history_postgres_scale):
    _, expected, _, _, cpu_elapsed = job_history_postgres_scale
    budget = python_budget(len(expected) / 12)
    assert cpu_elapsed < budget


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_job_history_postgres_wall_benchmark(job_history_postgres_scale):
    _, expected, _, elapsed, _ = job_history_postgres_scale
    budget = python_budget(len(expected) / 12)
    assert elapsed < budget * 4
