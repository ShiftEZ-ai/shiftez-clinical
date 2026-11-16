"""Truthful exhausted attempts and bounded lease/checkpoint reconstruction work."""
from copy import deepcopy
import re
import threading

import pytest
from ortools.sat.python import cp_model
from sqlalchemy import event, null, select, update

from apps.api import models as m
from apps.api.db import retry_write, transaction
from apps.api.serialization import job_read_metadata, job_result_projection, job_outcome
from apps.api.services.generation_resume import RESUMABLE_GENERATION_RESULTS
from apps.worker import main as worker
from packages.domain import solver_resume
from packages.domain.generation_outcome import generation_outcome
from packages.domain.solver_control import Cancelled
from tests.test_autoschedule_budget_worker import generation_job
from tests.test_autoschedule_budget_api import budget_h, start


@pytest.mark.parametrize('result_status', sorted(RESUMABLE_GENERATION_RESULTS))
@pytest.mark.parametrize('worker_status,validation,expected', [
    ('succeeded', None, 'needs_more_time'),
    ('succeeded', {'valid': True, 'error_count': 0}, 'complete'),
    ('succeeded', {'valid': False, 'error_count': 1}, 'partial'),
    ('failed', {'valid': True}, 'failed'),
    ('cancelled', {'valid': True}, 'cancelled'),
])
def test_resumable_outcomes_preserve_legacy_proposals_and_worker_failure(
        budget_h, result_status, worker_status, validation, expected):
    h = budget_h
    workspace, job = start(h)
    result = {'status': result_status, 'solver_status': 'UNKNOWN', 'additions': [], 'removals': []}
    if validation is not None:
        result['validation'] = validation
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.status, row.result = worker_status, result
    detail = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    history = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs']
    assert detail['outcome'] == next(row for row in history if row['id'] == job['id'])['outcome'] == expected
    assert detail['result'] == {**result, 'generation_outcome': generation_outcome(result)}


def test_unknown_without_validation_is_inconclusive_even_with_empty_legacy_check():
    assert job_outcome('succeeded', {'status': 'unknown_time_limit', 'validation': {'valid': None}}) == 'needs_more_time'
    assert job_outcome('running', {'status': 'unknown_time_limit'}) is None


def large_job(h):
    job_id = generation_job(h)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job_id)
        row.input_snapshot = {**row.input_snapshot, 'fictional_payload': 'synthetic snapshot ' * 150000}
        row.result = {'status': 'unknown_time_limit', 'candidate_evidence': [
            {'person_id': 'fictional', 'exclusions': []}] * 10000}
        row.resume_state = {'key': 'fictional', 'model': 'synthetic model ' * 150000}
        row.resume_checkpoint = {'key': 'fictional', 'hint': [0] * 10000}
    return job_id


def job_reads(statements):
    return [sql for sql in statements if sql.lstrip().startswith('SELECT') and 'FROM jobs' in sql]


def assert_narrow_lease(statements):
    reads = job_reads(statements)
    assert reads
    assert not any(re.search(r'jobs\.(input_snapshot|result|result_summary|resume_state|resume_checkpoint)\b', sql)
                   for sql in reads)


@pytest.mark.parametrize('legacy', [False, True])
def test_heartbeat_does_not_reread_large_payloads_and_backfills_legacy_metadata_once(h, legacy):
    job_id, engine = large_job(h), h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    assert claim['id'] == job_id
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        snapshot, result = deepcopy(row.input_snapshot), deepcopy(row.result)
    if legacy:
        with transaction(engine, write=True) as db:
            db.execute(update(m.Job).where(m.Job.id == job_id).values(read_metadata=null(), result_summary=null()))
    statements = []
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        assert retry_write(engine, lambda db: worker.heartbeat_job(db, claim, {'stage': 'finding'}))
        if legacy:
            assert any('jobs.input_snapshot' in sql for sql in job_reads(statements))
            assert any(re.search(r'jobs\.result\b', sql) for sql in job_reads(statements))
            assert not any('jobs.resume_state' in sql for sql in job_reads(statements))
        else:
            assert_narrow_lease(statements)
        statements.clear()
        assert retry_write(engine, lambda db: worker.heartbeat_job(db, claim,
            {'stage': 'improving', 'search_elapsed_seconds': 2}))
        assert_narrow_lease(statements)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.read_metadata == job_read_metadata(snapshot, result)
        assert row.result_summary == job_result_projection('generation', result)
        assert row.input_snapshot == snapshot and row.result == result
        assert row.search_control['spent_seconds'] == 2


def test_real_outer_lease_renewal_does_not_load_raw_payloads(budget_h, monkeypatch):
    h = budget_h
    job_id, engine = large_job(h), h.app.state.engine
    renewed, statements = threading.Event(), []
    main_thread = threading.get_ident()
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if threading.get_ident() != main_thread:
            statements.append(statement)
    monkeypatch.setattr(worker, 'GENERATION_HEARTBEAT_SECONDS', .01)
    monkeypatch.setattr(worker, 'heartbeat', lambda *args, **kwargs: renewed.set())
    def solving(snapshot, *, progress, cancelled):
        progress({'stage': 'improving', 'search_elapsed_seconds': 1})
        assert renewed.wait(5), 'The real lease monitor never completed a renewal'
        assert not cancelled()
        return {'status': 'needs_more_time', 'additions': [], 'removals': []}
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        assert worker.process_one(engine, scheduler=solving)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert_narrow_lease(statements)
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'succeeded' and row.search_control['spent_seconds'] == 1


@pytest.mark.parametrize('change', ['new_attempt', 'cancelled', 'disabled', 'revoked', 'stale'])
def test_narrow_heartbeat_keeps_ownership_access_and_current_input_checks(h, change):
    job_id, engine = generation_job(h), h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    with transaction(engine, write=True) as db:
        row = db.get(m.Job, job_id)
        row.resume_state, row.resume_checkpoint = {'model': 'fictional'}, {'hint': [1]}
        if change == 'new_attempt':
            row.attempts += 1
        elif change == 'cancelled':
            row.status = 'cancelled'
        elif change == 'disabled':
            db.get(m.Identity, row.requested_by).disabled = True
        elif change == 'revoked':
            for binding in db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == row.requested_by)):
                binding.revoked = True
        else:
            db.get(m.Workspace, row.workspace_id).content_revision += 1
    assert retry_write(engine, lambda db: worker.heartbeat_job(db, claim, {'stage': 'improving'})) == (change == 'stale')
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        if change in {'disabled', 'revoked'}:
            assert row.resume_state is None and row.resume_checkpoint is None
        else:
            assert row.resume_state == {'model': 'fictional'}
        assert row.progress['stage'] == ('improving' if change == 'stale' else 'preparing')


@pytest.mark.parametrize('compressed', [False, True])
@pytest.mark.parametrize('legacy_mapping', [False, True])
def test_prepared_roundtrip_preserves_model_expressions_and_legacy_text(compressed, legacy_mapping):
    model = cp_model.CpModel()
    x, y = model.new_int_var(0, 3, 'x'), model.new_bool_var('y')
    model.add(x >= y)
    prepared = {'model': model, 'vars': [x, y, x], 'expression': 2 * x + y + 3,
                'data': {'label': 'عيادة تجريبية', 'mixed': {(1, 'tuple'): {2, 3}}, '_check_budget': object()}}
    state = solver_resume.prepared_state('semantic-key', prepared)
    if legacy_mapping:
        state['prepared']['mapping'] = [[key, value] for key, value in state['prepared']['mapping'].items()]
    original = deepcopy(state)
    state = solver_resume.decompress_state(solver_resume.compress_state(state)) if compressed else state
    restored = solver_resume.restore(state, 'semantic-key')
    assert str(restored['model'].proto) == str(model.proto)
    assert restored['vars'][0] is restored['vars'][2]
    assert restored['data'] == {'label': 'عيادة تجريبية', 'mixed': {(1, 'tuple'): {2, 3}}}
    expression = cp_model.FlatIntExpr(restored['expression'])
    assert [variable.index for variable in expression.vars] == [x.index, y.index]
    assert list(expression.coeffs) == [2, 1] and expression.offset == 3
    assert solver_resume.restore(state, 'changed-input-or-version') is None
    assert state == original


def test_restore_reuses_only_this_models_referenced_variables(monkeypatch):
    model = cp_model.CpModel()
    x = model.new_int_var(0, 3, 'x')
    state = solver_resume.prepared_state('key', {'model': model, 'refs': [x] * 20, 'expr': x + 1})
    original, calls = cp_model.CpModel.get_int_var_from_proto_index, []
    def variable(self, index):
        calls.append((self, index))
        return original(self, index)
    monkeypatch.setattr(cp_model.CpModel, 'get_int_var_from_proto_index', variable)
    first, second = solver_resume.restore(state, 'key'), solver_resume.restore(state, 'key')
    assert calls == [(first['model'], 0), (second['model'], 0)]
    assert first['refs'][0] is not second['refs'][0]
    for invalid in ({**state, 'model': 'not a model'},
                    {**state, 'prepared': {'variable': 999}},
                    {**state, 'prepared': [{'variable': 0}, {'variable': 0.0}]}):
        with pytest.raises(ValueError):
            solver_resume.restore(invalid, 'key')


def test_cancelled_decode_does_not_read_the_compressed_payload(monkeypatch):
    state = solver_resume.compress_state({'key': 'key', 'prepared': {}})
    monkeypatch.setattr(solver_resume.base64, 'b64decode', lambda *a, **k: pytest.fail('Decoded after cancellation'))
    def cancelled():
        raise Cancelled()
    with pytest.raises(Cancelled):
        solver_resume.decompress_state(state, check=cancelled)


def test_cancelled_restore_after_validation_does_not_unpack(monkeypatch):
    model = cp_model.CpModel()
    model.new_bool_var('a')
    state = solver_resume.prepared_state('key', {'model': model})
    original, validated = cp_model.CpModel.validate, []
    def validate(self):
        result = original(self)
        validated.append(True)
        return result
    monkeypatch.setattr(cp_model.CpModel, 'validate', validate)
    monkeypatch.setattr(solver_resume, 'unpack', lambda *a, **k: pytest.fail('Unpacked after cancellation'))
    def cancelled():
        if validated:
            raise Cancelled()
    with pytest.raises(Cancelled):
        solver_resume.restore(state, 'key', check=cancelled)


def test_cancelled_restore_stops_reconstructing_variables(monkeypatch):
    model = cp_model.CpModel()
    state = solver_resume.prepared_state('key', {'model': model, 'vars': [model.new_bool_var('a'), model.new_bool_var('b')]})
    original, made = cp_model.CpModel.get_int_var_from_proto_index, []
    def variable(self, index):
        made.append(index)
        return original(self, index)
    monkeypatch.setattr(cp_model.CpModel, 'get_int_var_from_proto_index', variable)
    def cancelled():
        if made:
            raise Cancelled()
    with pytest.raises(Cancelled):
        solver_resume.restore(state, 'key', check=cancelled)
    assert made == [0]
