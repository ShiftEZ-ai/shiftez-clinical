"""Greedy omissions are disclosed without changing its assignment decisions."""
import copy
import logging
import sys
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.api import live, models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.main import create_app
from apps.api.services.jobs import create_job
from apps.worker import main as worker
from packages.contracts.rule_catalogue import GREEDY_NOT_APPLIED
from packages.contracts.schemas import Command, GenerationNotApplied, JobResult
from packages.domain import cp_sat, generation
from packages.domain.rule_migration_proof import _solve


def test_omissions_name_exactly_the_inputs_proved_by_f5():
    omissions = {item['input']: item for item in GREEDY_NOT_APPLIED}
    assert omissions['wishes']['message'] == 'Dated wishes are not used to select assignments.'
    assert {key: set(item['kinds']) for key, item in omissions.items()} == {
        'wishes': {'prefer_off', 'prefer_on', 'prefer_shift_type', 'avoid_shift_type'},
        'work_patterns': {'run_length', 'days_off_run', 'weekend_shape', 'shift_succession', 'together_apart'},
        'fairness_targets': {'balance_goal'},
        'advisory_limits': {'max_hours', 'max_duties', 'min_rest', 'forbidden_transition',
                            'max_consecutive_days', 'max_consecutive_weekends', 'travel_buffer',
                            'restriction', 'maximum_participants', 'block_quota'},
    }
    assert 'advisory maxima only' in omissions['advisory_limits']['message']
    assert 'Quota minima and targets' in omissions['advisory_limits']['message']
    assert 'lead-turn goals are still used' in omissions['fairness_targets']['message']
    schema = GenerationNotApplied.model_json_schema()
    assert set(schema['properties']['input']['enum']) == set(omissions)
    with pytest.raises(ValidationError):
        GenerationNotApplied(input='unsupported_input', kinds=[], message='Invalid input kind')


@pytest.mark.parametrize('status', ['candidate_feasible', 'candidate_with_gaps', 'unknown_time_limit',
                                   'unsupported_size', 'unsupported_period', 'unsupported_rules'])
def test_greedy_discloses_omissions_for_candidates_and_early_returns(h, status):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(workspace)
    budget = 5
    if status == 'candidate_with_gaps':
        data['people'] = {}
    elif status == 'unknown_time_limit':
        budget = 0
    elif status == 'unsupported_size':
        person = next(iter(data['people'].values()))
        data['people'] = {f'person-{i}': person for i in range(generation.MANIFEST['max_people'] + 1)}
    elif status == 'unsupported_period':
        data['shifts'][0]['end'] = '2026-12-04T12:00:00+03:00'
    elif status == 'unsupported_rules':
        data['policies'] = [{'id': 'unknown-rule', 'status': 'active', 'kind': 'unknown_kind',
                             'definition': {'rule': {'kind': 'unknown_kind'}}}]
    original = copy.deepcopy(data)
    result = generation.generate(data, budget)
    assert result['status'] == status, result
    assert result['not_applied'] == GREEDY_NOT_APPLIED
    assert data == original
    result['not_applied'][0]['kinds'].clear()
    assert GREEDY_NOT_APPLIED[0]['kinds']


@pytest.mark.parametrize('engine', ['legacy', 'cp_sat'])
def test_job_result_contract_keeps_engine_metadata_without_inventing_omissions(h, engine):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    result = (generation.generate if engine == 'legacy' else cp_sat.generate)(h.snapshot(workspace), 5)
    assert result['status'] == 'candidate_feasible', result
    decoded = JobResult.model_validate(result).model_dump(mode='json', exclude_unset=True)
    assert decoded == result
    assert bool(decoded.get('not_applied')) == (engine == 'legacy')
    assert decoded['additions']


@pytest.mark.parametrize(('engine', 'historical'), [('legacy', False), ('legacy', True), ('cp_sat', False)])
def test_job_detail_and_history_disclose_only_greedy_omissions(h, engine, historical):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=workspace['revision'],
            payload={'workspace_id': workspace['id'], 'kind': 'generation'}),
            replace(h.settings, generation_engine=engine))['job']
    assert h.process_jobs(solver_workers=1)
    if historical:
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Job, job['id'])
            row.result = {key: value for key, value in row.result.items() if key != 'not_applied'}
            row.input_snapshot = {key: value for key, value in row.input_snapshot.items() if key != 'generation_engine'}
    detail = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    history = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs'][0]
    assert detail['status'] == history['status'] == 'succeeded'
    for value in (detail, history):
        assert value['generation_engine'] == engine
        if engine == 'legacy':
            assert value['result']['not_applied'] == GREEDY_NOT_APPLIED
        else:
            assert not value['result'].get('not_applied')
    assert detail['result']['additions'] and 'additions' not in history['result']
    if historical:
        with transaction(h.app.state.engine) as db:
            assert 'not_applied' not in db.get(m.Job, job['id']).result


@pytest.mark.parametrize('engine', ['legacy', 'cp_sat'])
def test_harness_process_jobs_uses_the_queued_generation_engine(h, engine):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=workspace['revision'],
            payload={'workspace_id': workspace['id'], 'kind': 'generation'}),
            replace(h.settings, generation_engine=engine))['job']
    assert h.process_jobs(solver_workers=1)
    completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert completed['status'] == 'succeeded' and completed['generation_engine'] == engine


def test_harness_rejects_mixed_queued_generation_engines(h):
    first, second = h.workspace('First engine'), h.workspace('Second engine')
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        for workspace, engine in ((first, 'legacy'), (second, 'cp_sat')):
            create_job(db, actor, Command(kind='job.create', expected_revision=workspace['revision'],
                payload={'workspace_id': workspace['id'], 'kind': 'generation'}),
                replace(h.settings, generation_engine=engine))
    with pytest.raises(AssertionError, match='different engines'):
        h.process_jobs()


@pytest.mark.parametrize(('job_engine', 'worker_engine'), [('legacy', 'cp_sat'), ('cp_sat', 'legacy')])
def test_direct_process_one_keeps_the_same_engine_guard(h, job_engine, worker_engine):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=workspace['revision'],
            payload={'workspace_id': workspace['id'], 'kind': 'generation'}),
            replace(h.settings, generation_engine=job_engine))['job']
    # Deliberately bypass the harness helper to prove production rejects a mismatched scheduler.
    assert worker.process_one(h.app.state.engine,
                              settings=replace(h.settings, generation_engine=worker_engine), solver_workers=1)
    failed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert failed['status'] == 'failed' and failed['error_code'] == 'STALE_SCHEDULER'


def test_strict_migration_proof_discloses_greedy_comparison_omissions(h):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(workspace)
    proof = _solve(data, copy.deepcopy(data), seconds=5)
    assert proof['greedy']['not_applied'] == GREEDY_NOT_APPLIED
    assert 'not_applied' not in proof['cp_sat']
    assert all(part['conclusive'] for part in proof.values())


@pytest.mark.parametrize('entrypoint', ['api', 'worker'])
@pytest.mark.parametrize(('environment', 'engine', 'warns'), [
    ('development', None, True), ('test', None, False),
    ('development', 'legacy', False), ('development', 'cp_sat', False),
])
def test_startup_warns_once_only_for_implicit_greedy_outside_tests(h, monkeypatch, caplog,
                                                                entrypoint, environment, engine, warns):
    monkeypatch.setenv('ROTA_ENV', environment)
    if engine is None:
        monkeypatch.delenv('ROTA_GENERATION_ENGINE', raising=False)
    else:
        monkeypatch.setenv('ROTA_GENERATION_ENGINE', engine)
    settings = replace(h.settings, environment=environment, generation_engine=engine or 'legacy', fmuc_data_key='01' * 32)
    with caplog.at_level(logging.WARNING, logger='rota.settings'):
        if entrypoint == 'api':
            monkeypatch.setattr(live, 'attach', lambda loop: None)
            monkeypatch.setattr(live, 'listen', lambda url: None)
            monkeypatch.setattr(live, 'detach', lambda loop: None)
            with TestClient(create_app(settings, engine=h.app.state.engine)):
                pass
        else:
            monkeypatch.setenv('DATABASE_URL', h.settings.database_url)
            monkeypatch.delenv('DATABASE_URL_FILE', raising=False)
            monkeypatch.setenv('ROTA_SOLVER_WORKERS', '1')
            monkeypatch.setattr(sys, 'argv', ['worker', '--once'])
            monkeypatch.setattr(worker, 'configure_logging', lambda: None)
            def worker_engine(url, *, settings):
                assert settings.database_url == url == h.settings.database_url
                assert settings.environment == environment
                assert settings.generation_engine == (engine or 'legacy')
                return h.app.state.engine
            monkeypatch.setattr(worker, 'make_engine', worker_engine)
            monkeypatch.setattr(worker, 'process_one', lambda engine, **kwargs: False)
            worker.main()
    warnings = [record for record in caplog.records if record.name == 'rota.settings'
                and record.levelno == logging.WARNING]
    assert len(warnings) == int(warns)
    if warns:
        message = warnings[0].getMessage()
        assert 'ROTA_GENERATION_ENGINE is unset' in message and 'legacy greedy' in message
        assert 'ROTA_GENERATION_ENGINE=cp_sat' in message
        assert all(item['message'] in message for item in GREEDY_NOT_APPLIED)
