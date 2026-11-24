"""Retained generation evidence reads across both storage formats."""
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import event, func, insert, inspect, select
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.db import make_engine, retry_write, transaction
from apps.api.serialization import CANDIDATE_EVIDENCE_REF, CANDIDATE_EVIDENCE_VERSION, job_result_projection
from apps.worker.generation_preparation import candidate_evidence_for_followup
from apps.worker.main import claim_job, finish_job
from migrations import safety
from packages.domain.input_manifest import digest
from scripts import manage
from .conftest import postgres_database
from .test_autosched_reuse_p0 import followup
from .test_autoschedule_budget_worker import generation_job


@pytest.fixture(params=['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)])
def migration_url(request, tmp_path):
    if request.param == 'sqlite':
        yield 'sqlite:///' + str(tmp_path / 'evidence.db')
    else:
        with postgres_database(request.getfixturevalue('postgres_worker_url'), 'rota_test_' + uuid4().hex) as url:
            yield url


def test_candidate_evidence_migration_is_additive(migration_url):
    path = safety.VERSIONS / '0151_job_candidate_evidence.py'
    assert safety.classification(path) == 'additive'
    new = safety.VERSIONS / '0152_feasibility_results.py'
    assert safety.classification(new) == 'additive'
    evidence, feasibility = safety.revision(path), safety.revision(new)
    pending = manage.check_migrations(migration_url)
    assert pending[pending.index(evidence):pending.index(feasibility) + 1] == [evidence, feasibility]
    config = manage.alembic_config(migration_url)
    command.upgrade(config, '0148_bulk_analyze')
    pending = manage.check_migrations(migration_url)
    assert pending[:pending.index(feasibility) + 1] == ['0150_person_inactive_from', evidence, feasibility]
    command.upgrade(config, '0150_person_inactive_from')
    engine = make_engine(migration_url)
    try:
        before = set(inspect(engine).get_table_names())
        command.upgrade(config, evidence)
        schema = inspect(engine)
        assert set(schema.get_table_names()) - before == {'job_candidate_evidence'}
        assert before <= set(schema.get_table_names())
        assert schema.get_pk_constraint('job_candidate_evidence')['constrained_columns'] == ['job_id', 'ordinal']
        [index] = schema.get_indexes('job_candidate_evidence')
        assert index['column_names'] == ['job_id', 'shift_id', 'ordinal']
        [owner] = schema.get_foreign_keys('job_candidate_evidence')
        assert owner['constrained_columns'] == ['job_id'] and owner['referred_table'] == 'jobs'
        with engine.connect() as connection:
            assert not connection.execute(select(m.JobCandidateEvidence)).first()
        before = set(schema.get_table_names())
        command.upgrade(config, feasibility)
        schema = inspect(engine)
        assert set(schema.get_table_names()) == before | {'job_feasibility_results'}
        assert schema.get_pk_constraint('job_feasibility_results')['constrained_columns'] == ['job_id', 'attempt']
        [owner] = schema.get_foreign_keys('job_feasibility_results')
        assert owner['constrained_columns'] == ['job_id'] and owner['referred_table'] == 'jobs'
        with engine.connect() as connection:
            assert not connection.execute(select(m.JobFeasibilityResult)).first()
    finally:
        engine.dispose()


def attach_evidence(db, job, rows, *, stored=True):
    reference = {'version': CANDIDATE_EVIDENCE_VERSION, 'count': len(rows), 'digest': digest(rows)}
    job.result = {key: value for key, value in job.result.items() if key != 'candidate_evidence'}
    job.result = {**job.result, CANDIDATE_EVIDENCE_REF: reference}
    job.result_summary = job_result_projection(job.kind, {**job.result, 'candidate_evidence': rows})
    if stored:
        db.add_all(m.JobCandidateEvidence(job_id=job.id, ordinal=index,
            shift_id=row.get('shift_id'), payload=row) for index, row in enumerate(rows))


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_evidence_formats_return_the_same_pages_and_permissions(request, backend):
    h = request.getfixturevalue(backend)
    workspace = h.workspace()
    rows = [{'person_id': str(index), 'shift_id': 'clinic' if index % 2 else 'other',
             'exclusions': [{'code': 'RESTRICTION'}, {'code': 'RESTRICTION'}] if index % 3 else []}
            for index in range(11)]
    with transaction(h.app.state.engine, write=True) as db:
        for identifier in ('inline-evidence', 'external-evidence', 'missing-evidence'):
            job = m.Job(id=identifier, department_id=h.seed['department_id'], workspace_id=workspace['id'],
                requested_by=h.seed['identities']['coordinator'], kind='generation', status='succeeded',
                input_fingerprint='frozen', input_snapshot={}, result={'status': 'requirements_conflict',
                                                                       'candidate_evidence': rows})
            db.add(job)
            db.flush()
            if identifier != 'inline-evidence':
                attach_evidence(db, job, rows, stored=identifier == 'external-evidence')
    inline, external = ('/api/jobs/' + name for name in ('inline-evidence', 'external-evidence'))
    with transaction(h.app.state.engine, write=True) as db:
        # A later result update must preserve the retained evidence projection.
        job = db.get(m.Job, 'external-evidence')
        job.result = {**job.result, 'score': None}
    for suffix in ('', '?full=true'):
        for url in (inline, external):
            result = h.get('coordinator', url + suffix)['job']['result']
            assert result['candidate_evidence_count'] == len(rows)
            assert 'candidate_evidence' not in result and CANDIDATE_EVIDENCE_REF not in result
    for query, matching in (('offset=2&limit=3', rows), ('shift_id=clinic&offset=2&limit=3', rows[1::2]),
                            ('shift_id=absent&offset=2&limit=3', [])):
        old, new = (h.get('coordinator', path + '/evidence?' + query) for path in (inline, external))
        assert {key: value for key, value in old.items() if key != 'job_id'} == {
            key: value for key, value in new.items() if key != 'job_id'}
        assert new['candidates'] == matching[2:5] and new['total'] == len(matching)
    for role in ('staff', 'approver'):
        h.get(role, external + '/evidence', status=403)
    problem = h.get('coordinator', '/api/jobs/missing-evidence/evidence', status=404)
    assert problem['error']['code'] == 'EVIDENCE_NOT_FOUND'


def test_followup_reuses_exact_evidence_in_both_formats(followup):
    engine, claim, rows = followup
    assert candidate_evidence_for_followup(engine, claim) == rows
    m.JobCandidateEvidence.__table__.create(engine)
    with Session(engine) as db, db.begin():
        attach_evidence(db, db.get(m.Job, 'strict'), rows)
    assert candidate_evidence_for_followup(engine, claim) == rows
    with Session(engine) as db, db.begin():
        row = db.get(m.JobCandidateEvidence, ('strict', 0))
        row.payload = {**row.payload, 'exclusions': []}
    assert candidate_evidence_for_followup(engine, claim) is None


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_worker_completion_binds_evidence_under_its_lease(request, backend):
    h = request.getfixturevalue(backend)
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, claim_job)
    rows = [{'shift_id': 'clinic', 'person_id': 'selected', 'exclusions': []},
            {'shift_id': 'clinic', 'person_id': 'unavailable', 'exclusions': [{'code': 'UNAVAILABLE'}]}]
    proposal = {'status': 'candidate_feasible', 'input_fingerprint': claim['input']['fingerprint'],
                'additions': [], 'removals': [], 'candidate_evidence': rows}
    stored = select(m.JobCandidateEvidence.payload).where(m.JobCandidateEvidence.job_id == job_id).order_by(
        m.JobCandidateEvidence.ordinal)
    assert not retry_write(engine, lambda db: finish_job(db, {**claim, 'attempt': claim['attempt'] + 1}, proposal, None))
    with transaction(engine) as db:
        assert db.get(m.Job, job_id).status == 'running'
        assert not db.scalars(stored).all()
    assert retry_write(engine, lambda db: finish_job(db, claim, proposal, None))
    assert not retry_write(engine, lambda db: finish_job(db, claim, proposal, None))
    with transaction(engine) as db:
        job = db.get(m.Job, job_id)
        assert job.status == 'succeeded' and 'candidate_evidence' not in job.result
        reference = {'version': CANDIDATE_EVIDENCE_VERSION, 'count': len(rows), 'digest': digest(rows)}
        assert job.result[CANDIDATE_EVIDENCE_REF] == reference
        manifest = job.input_snapshot['generation_manifest']['result']
        assert manifest[CANDIDATE_EVIDENCE_REF] == reference and manifest['hash'] == digest(job.result)
        assert list(db.scalars(stored)) == rows
    page = h.get('coordinator', f'/api/jobs/{job_id}/evidence?shift_id=clinic')
    assert page['candidates'] == rows and page['total'] == 2 and page['eligible'] == 1


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_worker_evidence_matches_insert_and_rolls_back(request, backend):
    h = request.getfixturevalue(backend)
    engine = h.app.state.engine
    job_id = generation_job(h)
    claim = retry_write(engine, claim_job)
    rows = [
        {'shift_id': 'clinic', 'person_id': 'first', 'exclusions': []},
        {'shift_id': None, 'person_id': 'second', 'exclusions': [{'code': 'UNAVAILABLE',
                                                               'detail': 'line\n\t\\ "\u2603"'}]},
        {'person_id': 'third', 'exclusions': [], 'eligible': False},
    ]
    proposal = {'status': 'candidate_feasible', 'input_fingerprint': claim['input']['fingerprint'],
                'additions': [], 'removals': [], 'candidate_evidence': rows}
    evidence = m.JobCandidateEvidence
    stored = select(evidence.ordinal, evidence.shift_id, evidence.payload).where(
        evidence.job_id == job_id).order_by(evidence.ordinal)
    with transaction(engine, write=True) as db:
        job = db.get(m.Job, job_id)
        before = job.result, job.input_snapshot
        with db.begin_nested() as reference_write:
            db.connection().execute(insert(evidence.__table__), [
                {'job_id': job_id, 'ordinal': index, 'shift_id': row.get('shift_id'), 'payload': row}
                for index, row in enumerate(rows)])
            reference = db.execute(stored).all()
            reference_write.rollback()

    evidence_inserts = []

    @event.listens_for(engine, 'before_cursor_execute')
    def observe_insert(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith('INSERT INTO job_candidate_evidence '):
            evidence_inserts.append(statement)

    def fail_after_finish(db):
        assert finish_job(db, claim, proposal, None)
        db.flush()
        assert db.get(m.Job, job_id).status == 'succeeded'
        assert db.execute(stored).all() == reference
        raise RuntimeError('forced completion failure')

    with pytest.raises(RuntimeError, match='forced completion failure'):
        retry_write(engine, fail_after_finish)
    with transaction(engine) as db:
        job = db.get(m.Job, job_id)
        assert job.status == 'running' and job.completed_at is None
        assert (job.result, job.input_snapshot) == before
        assert db.execute(stored).all() == []

    assert retry_write(engine, lambda db: finish_job(db, claim, proposal, None))
    with transaction(engine) as db:
        job = db.get(m.Job, job_id)
        actual = db.execute(stored).all()
        assert actual == reference
        assert actual == [(index, row.get('shift_id'), row) for index, row in enumerate(rows)]
        expected_digest = digest([row.payload for row in reference])
        assert job.result[CANDIDATE_EVIDENCE_REF]['digest'] == expected_digest
        assert job.input_snapshot['generation_manifest']['result'][CANDIDATE_EVIDENCE_REF]['digest'] == expected_digest
    assert len(evidence_inserts) == (2 if backend == 'h' else 0)


def test_job_list_reports_stored_candidate_evidence_count(h):
    engine = h.app.state.engine
    job_id = generation_job(h)
    claim = retry_write(engine, claim_job)
    rows = [{'shift_id': 'clinic', 'person_id': str(index), 'exclusions': []} for index in range(2)]
    proposal = {'status': 'candidate_feasible', 'input_fingerprint': claim['input']['fingerprint'],
                'additions': [], 'removals': [], 'candidate_evidence': rows}
    assert retry_write(engine, lambda db: finish_job(db, claim, proposal, None))

    evidence = m.JobCandidateEvidence
    with transaction(engine) as db:
        stored_count = db.scalar(select(func.count()).select_from(evidence).where(evidence.job_id == job_id))
    listed = next(job for job in h.get('coordinator', '/api/jobs')['jobs'] if job['id'] == job_id)
    assert listed['result']['candidate_evidence_count'] == stored_count == len(rows)
