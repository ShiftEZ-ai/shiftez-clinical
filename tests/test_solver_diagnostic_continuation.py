"""An incomplete proposal continues the strict search without rebuilding its inputs."""
import copy
import time
from dataclasses import replace

import pytest

from packages.contracts.schemas import GenerationOptions
from packages.domain import cp_sat, solver_resume
from packages.domain.solver_control import SolverControl
from packages.domain.validation import finding_observations
from tests.test_scheduler_performance import _budget
from tests.test_solver_search_budget import store_checkpoints


def shortfall(h, *, quota=False, people=None, cover_maximum=1):
    person = h.seed['people'][0]
    typ = h.configure('assignment_types', {
        'name': 'Synthetic bounded shortfall', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': cover_maximum}]}})['record']['id']
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    if quota:
        h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
                  'metric': 'duties', 'comparison': 'minimum', 'amount': 2}, {'person_id': person})
    data = h.snapshot(h.shift(h.workspace(), typ=typ, people=[])['workspace'])
    selected_people = [person] if people is None else list(people)
    data['people'] = {pid: data['people'][pid] for pid in selected_people}
    return data


@pytest.mark.parametrize('quota', [False, True])
def test_diagnostic_continues_once_and_keeps_maximum_and_blocking_findings(h, monkeypatch, quota):
    data = shortfall(h, quota=quota)
    original = cp_sat._prepare
    preparations = []

    def prepare(*args, **kwargs):
        started = time.process_time()
        result = original(*args, **kwargs)
        preparations.append(time.process_time() - started)
        return result

    monkeypatch.setattr(cp_sat, '_prepare', prepare)
    progress = []
    result = cp_sat.generate(data, 3, deterministic=True, _diagnostic_continuation=True, progress=progress.append)
    assert result['status'] == 'candidate_with_gaps', result
    assert result['diagnostic_proposal'] and result['diagnostic_continued'] and result['preparation_reused']
    assert result['conflict']['strict_status'] == 'INFEASIBLE' and result['conflict']['proven']
    assert len(preparations) == 1
    assert any(not row['diagnostic_proposal'] for row in progress if row['stage'] == 'preparing')
    assert any(row['diagnostic_proposal'] for row in progress if row['stage'] == 'ready')
    assert len(result['additions']) == 1 and not result['removals']
    assert not result['validation']['valid'] and not result['optimality_proven']
    codes = {f['code'] for f in finding_observations(result['validation']) if f['severity'] == 'error'}
    assert 'SCOPED_COVERAGE_GAP' in codes
    assert ('QUOTA_BELOW_MINIMUM' in codes) is quota
    if quota:
        assert next(row for row in result['objectives'] if row['name'] == 'diagnostic_minima')['value'] > 0
    assert 'MAXIMUM_PARTICIPANTS' not in codes
    print('single diagnostic preparation CPU seconds', preparations[0])


def test_diagnostic_continuation_survives_checkpoint_and_never_reprepares(h, monkeypatch):
    data, saved = shortfall(h, quota=True), []
    original = cp_sat.generate(data, 3, deterministic=True, checkpoint=store_checkpoints(saved), _diagnostic_continuation=True)
    assert original['status'] == 'candidate_with_gaps', original
    monkeypatch.setattr(cp_sat, '_prepare', lambda *args: pytest.fail('Diagnostic continuation rebuilt preparation'))
    progress = []
    resumed = cp_sat.generate(data, 3, deterministic=True, resume_state=solver_resume.compress_state(saved[0]), progress=progress.append)
    assert resumed['status'] == 'candidate_with_gaps', resumed
    assert resumed['preparation_reused'] and resumed['diagnostic_proposal']
    assert resumed['conflict'] == original['conflict']
    assert progress and all(row['diagnostic_proposal'] for row in progress)
    assert resumed['additions'] == original['additions'] and resumed['removals'] == original['removals']


def test_preparation_preserves_live_coverage_and_rebuilds_format_four(h, monkeypatch):
    data, options = shortfall(h, quota=True), GenerationOptions()
    prepared = cp_sat._prepare(data, 3, 1, SolverControl(), options, False, False)
    key = solver_resume.resume_key(data, options, False, False)
    restored = solver_resume.restore(solver_resume.prepared_state(key, prepared), key)
    assert restored['live_coverage']
    assert solver_resume.pack(restored['live_coverage']) == solver_resume.pack(prepared['live_coverage'])
    with monkeypatch.context() as legacy:
        legacy.setattr(solver_resume, 'PREPARATION_FORMAT', 4)
        key = solver_resume.resume_key(data, options, False, False)
    state = solver_resume.prepared_state(key, {name: value for name, value in prepared.items()
                                              if name != 'live_coverage'})
    assert solver_resume.restore(state, solver_resume.resume_key(data, options, False, False)) is None
    original, preparations = cp_sat._prepare, []

    def prepare(*args, **kwargs):
        current = original(*args, **kwargs)
        assert current['live_coverage']
        preparations.append(True)
        return current

    monkeypatch.setattr(cp_sat, '_prepare', prepare)
    result = cp_sat.generate(data, 3, deterministic=True, _diagnostic_continuation=True,
                             resume_state=solver_resume.compress_state(state))
    assert preparations == [True]
    assert result['status'] == 'candidate_with_gaps', result
    assert result['diagnostic_continued'] and not result['validation']['valid']


def test_worker_checkpoint_keeps_diagnostic_mode_without_rewriting_preparation(h, monkeypatch):
    from apps.api import models as m
    from apps.api.db import retry_write, transaction
    from apps.worker import main as worker
    from apps.worker.main import prepare_deferred_generation
    from packages.domain.solver_rules import COMPILER_VERSION

    data = shortfall(h, quota=True)
    workspace = h.detail(data['workspace']['id'])['workspace']
    job_id = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
                       'generation_options': {'on_conflict': 'continue_flagged'}},
                       revision=workspace['revision'])['job']['id']
    engine = h.app.state.engine
    with transaction(engine, write=True) as db:
        job = db.get(m.Job, job_id)
        job.input_snapshot = {**job.input_snapshot, 'generation_engine': 'cp_sat', 'compiler_version': COMPILER_VERSION}
    claim = retry_write(engine, worker.claim_job)
    prepare_deferred_generation(engine, claim, replace(h.settings, generation_engine='cp_sat'))
    options = GenerationOptions.model_validate(claim['input'].get('generation_options', {}))
    key = solver_resume.resume_key(claim['input'], options, False, False)
    prepared = cp_sat._prepare(claim['input'], 3, 1, SolverControl(), options, False, False)
    state = solver_resume.prepared_state(key, prepared)
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim, state))
    with transaction(engine) as db:
        original = copy.deepcopy(db.get(m.Job, job_id).resume_state)
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim, {
        'key': key, 'diagnostic_proposal': True, 'hint': [], 'has_solution': False}))
    with transaction(engine) as db:
        job = db.get(m.Job, job_id)
        assert job.resume_state == original
        assert job.resume_checkpoint['diagnostic_proposal']
        resume = {**job.resume_state, 'checkpoint': job.resume_checkpoint}
    monkeypatch.setattr(cp_sat, '_prepare', lambda *args: pytest.fail('Recovered worker rebuilt preparation'))
    result = cp_sat.generate(claim['input'], 3, deterministic=True, resume_state=resume)
    assert result['status'] == 'candidate_with_gaps', result
    assert result['preparation_reused'] and result['diagnostic_proposal']
    assert not result['validation']['valid']


def test_feasibility_model_never_relaxes_the_minimum(h):
    result = cp_sat.generate(shortfall(h, quota=True), 3, feasibility_only=True, deterministic=True, _diagnostic_continuation=True)
    assert result['status'] == 'requirements_conflict', result
    assert not result['additions'] and not result['removals']
    assert not result['diagnostic_proposal']


def test_diagnostic_continuation_never_relaxes_a_mandatory_people_ceiling(h):
    data = shortfall(h)
    h.policy({'kind': 'maximum_participants', 'maximum': 0})
    data = h.snapshot(h.detail(data['workspace']['id'])['workspace'])
    result = cp_sat.generate(data, 3, deterministic=True, _diagnostic_continuation=True)
    assert result['status'] == 'candidate_with_gaps', result
    assert result['diagnostic_proposal'] and result['diagnostic_continued']
    assert not result['additions'] and not result['removals']
    codes = {f['code'] for f in finding_observations(result['validation']) if f['severity'] == 'error'}
    assert 'COVERAGE_GAP' in codes and 'SCOPED_COVERAGE_GAP' in codes
    assert 'SCOPED_PARTICIPANT_LIMIT' not in codes


def test_coverage_search_does_not_plateau_stop_with_unfilled_places(h, monkeypatch):
    data = shortfall(h)
    original = SolverControl.solve
    checked = []

    def solve(control, solver, model, tier):
        if tier == 0:
            checked.append(control.stall_seconds)
        return original(control, solver, model, tier)

    monkeypatch.setattr(SolverControl, 'solve', solve)
    result = cp_sat.generate(data, 3, diagnostic_proposal=True)
    assert result['status'] == 'candidate_with_gaps', result
    assert checked and all(value is None for value in checked)


def test_worker_generation_enables_same_job_diagnostic_continuation(h, monkeypatch):
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.worker import main as worker
    from packages.domain.solver_rules import COMPILER_VERSION

    data = shortfall(h, quota=True)
    workspace = h.detail(data['workspace']['id'])['workspace']
    job_id = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
                       'generation_options': {'on_conflict': 'continue_flagged'}},
                       revision=workspace['revision'])['job']['id']
    engine = h.app.state.engine
    with transaction(engine, write=True) as db:
        job = db.get(m.Job, job_id)
        job.input_snapshot = {**job.input_snapshot, 'generation_engine': 'cp_sat', 'compiler_version': COMPILER_VERSION}
    generate = cp_sat.generate
    calls = []

    def run(snapshot, **kwargs):
        assert kwargs['_diagnostic_continuation']
        calls.append(snapshot['workspace']['id'])
        kwargs['workers'] = 1
        return generate(snapshot, 3, deterministic=True, **kwargs)

    monkeypatch.setattr(cp_sat, 'generate', run)
    assert worker.process_one(engine, settings=replace(h.settings, generation_engine='cp_sat'))
    with transaction(engine) as db:
        job = db.get(m.Job, job_id)
        assert job.status == 'succeeded', job.error_code
        result = job.result
    assert calls == [workspace['id']]
    assert result['status'] == 'candidate_with_gaps', result
    assert result['diagnostic_continued'] and result['preparation_reused']
    assert not result['validation']['valid'] and not result['optimality_proven']


@pytest.mark.cpu_budget
def test_reused_diagnostic_preparation_cpu_budget(h):
    data = shortfall(h, quota=True)
    options, control = GenerationOptions(), SolverControl()
    started = time.process_time()
    prepared = cp_sat._prepare(copy.deepcopy(data), 3, 1, control, options, False, False)
    preparation_seconds = time.process_time() - started
    key = solver_resume.resume_key(data, options, False, False)
    assert key == solver_resume.resume_key(data, options, False, True)
    assert key == solver_resume.resume_key({**data, 'diagnostic_proposal': True}, options, False, True)
    state = solver_resume.prepared_state(key, prepared)
    original = str(prepared['model'].proto)
    started = time.process_time()
    restored = solver_resume.restore(state, key)
    cp_sat._diagnostic_mode(restored, True)
    elapsed = time.process_time() - started
    assert restored['result']['diagnostic_proposal']
    assert len(restored['model'].proto.variables) == len(prepared['model'].proto.variables)
    assert str(restored['model'].proto) != original
    assert elapsed < _budget(1), elapsed
    print('diagnostic preparation vs restore/domain switch CPU seconds', preparation_seconds, elapsed)
