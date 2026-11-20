"""Search controls keep saved preparation private and preserve an immutable attempt history."""
from .clock_support import freeze_clock
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from threading import Event, Thread
from time import monotonic

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, inspect, null, or_, select, text, update

from apps.api import models as m
from apps.api.db import transaction
from apps.api.main import create_app
from apps.api.serialization import job_record, record
from apps.api.snapshots import domain_snapshot
from apps.api.workspace_baselines import verify_generation_snapshot
from apps.worker.main import claim_job, prepare_deferred_generation, process_one
from packages.contracts.schemas import AUTO_SCHEDULE_LIMITS, GENERATION_SEARCH_FINISHED_STAGES, ROLE_NAMES, GenerationOptions
from .test_staff_wishes import department_settings


@pytest.fixture
def budget_h(h):
    h.app.state.engine.dispose()
    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings)
    return h


def start(h, options=None):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    payload = {'workspace_id': workspace['id'], 'kind': 'generation'}
    if options is not None:
        payload['generation_options'] = options
    job = h.command('coordinator', 'job.create', payload, revision=workspace['revision'])['job']
    with transaction(h.app.state.engine, write=True) as db:
        claim = claim_job(db)
    prepare_deferred_generation(h.app.state.engine, claim, h.settings)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.status, row.attempts = 'queued', 0
        row.started_at = row.heartbeat_at = row.worker_id = row.progress = None
        job = {**job, **job_record(row)}
    return workspace, job


def test_deferred_generation_reads_use_the_frozen_fingerprint_only_after_preparation(budget_h):
    h = budget_h
    workspace = h.shift(h.workspace(), people=[])['workspace']
    queued = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
                       revision=workspace['revision'])['job']
    assert queued['input_fingerprint'] is None
    with transaction(h.app.state.engine) as db:
        retained = db.get(m.Job, queued['id']).read_metadata
        assert db.get(m.Job, queued['id']).supersedes is not None
        assert retained['input_snapshot']['_deferred_generation'] is True
        assert retained['input_fingerprint'] is None

    def reads(expected_fingerprint, expected_currency):
        detail = h.get('coordinator', '/api/jobs/' + queued['id'])['job']
        listed = next(row for row in h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs']
                      if row['id'] == queued['id'])
        status = h.get('coordinator', '/api/jobs/' + queued['id'] + '/status')['job']
        assert all(row['input_fingerprint'] == expected_fingerprint for row in (detail, listed, status)), [
            (row['currency'], row['input_fingerprint']) for row in (detail, listed, status)]
        if expected_currency is not None:
            assert all(row['currency'] == expected_currency for row in (detail, listed, status))
        return detail, listed, status

    reads(None, 'preparing')
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(update(m.Job).where(m.Job.id == queued['id']).values(read_metadata=null()))
    with transaction(h.app.state.engine) as db:
        from apps.api.services.job_reads import status_metadata
        projection = status_metadata(db, queued['id'])
        assert projection['input_fingerprint'] is None
    # A missing retained projection must not expose the request token from jobs.input_fingerprint.
    reads(None, 'preparing')

    with transaction(h.app.state.engine, write=True) as db:
        claim = claim_job(db)
        request_token = db.get(m.Job, queued['id']).input_fingerprint
    prepare_deferred_generation(h.app.state.engine, claim, h.settings)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, queued['id'])
        row.status, row.result = 'succeeded', {
            'status': 'candidate_feasible', 'algorithm_version': row.input_snapshot['compiler_version'],
            'additions': [], 'removals': []}
        frozen = row.input_fingerprint
    current = h.detail(workspace)['current_fingerprint']
    assert frozen == current and frozen != request_token
    detail, listed, status = reads(current, 'current')
    assert detail['result_stale'] is False and listed['result_stale'] is False and status['result_stale'] is False

    changed = h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])
    changed_current = h.detail(changed['workspace'])['current_fingerprint']
    detail, listed, status = reads(frozen, None)
    assert changed_current != frozen and detail['input_fingerprint'] == frozen
    assert all(row['currency'] in {'revalidation_required', 'unknown'} for row in (detail, listed, status))
    assert detail['result_stale'] is True and status['result_stale'] is True
    assert listed['result_stale'] in {True, None}


def exhausted(h, job, state=None):
    checkpoint = {'key': 'private-checkpoint', 'hint': [0, 1, 1], 'has_solution': False} if state else None
    with transaction(h.app.state.engine) as db:
        needs_freeze = 'compiler_version' not in (db.get(m.Job, job['id']).input_snapshot or {})
    if needs_freeze:
        with transaction(h.app.state.engine, write=True) as db:
            claim = claim_job(db)
        prepare_deferred_generation(h.app.state.engine, claim, h.settings)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.status, row.completed_at = 'succeeded', m.now()
        row.result = {'status': 'needs_more_time', 'algorithm_version': row.input_snapshot['compiler_version']}
        row.resume_state = deepcopy(state)
        row.resume_checkpoint = checkpoint
    return checkpoint


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_reload_keeps_generation_current_after_a_later_rota_check(request, backend):
    h = request.getfixturevalue(backend)
    h.app.state.engine.dispose()
    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings)
    workspace, generation = start(h)
    exhausted(h, generation)
    check = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'validation'},
                      revision=workspace['revision'])['job']
    assert process_one(h.app.state.engine)
    for _ in range(2):
        rows = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs']
        assert rows[0]['id'] == check['id'] and rows[0]['status'] == 'succeeded'
        retained = next(row for row in rows if row['id'] == generation['id'])
        assert retained['result_stale'] is False and retained['currency'] == 'current'
    h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])
    rows = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs']
    retained = next(row for row in rows if row['id'] == generation['id'])
    assert retained['result_stale'] is True and retained['currency'] != 'current'


@pytest.mark.parametrize('seconds,minutes', [(300, 5), (600, 10)])
def test_legacy_search_budget_is_normalized_without_changing_stored_evidence(budget_h, seconds, minutes):
    h = budget_h
    options = GenerationOptions.model_validate({'search_seconds': seconds})
    assert options.search_minutes == minutes
    assert options.model_dump(exclude_unset=True) == {'search_minutes': minutes}
    workspace, job = start(h, {'search_seconds': seconds})
    assert job['effective_options']['search_minutes'] == minutes
    assert 'search_seconds' not in job['effective_options']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.input_snapshot = {**row.input_snapshot, 'generation_options': {'search_seconds': seconds}}
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['effective_options'] == {'search_minutes': minutes}
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job['id']).input_snapshot['generation_options'] == {'search_seconds': seconds}


@pytest.mark.parametrize('minutes', [0, -1, 1.5, True, '5', AUTO_SCHEDULE_LIMITS['maximum_minutes'] + 1])
def test_search_minutes_are_bounded_integers(minutes):
    with pytest.raises(ValidationError):
        GenerationOptions(search_minutes=minutes)


def test_department_search_maximum_is_audited_undoable_and_cannot_be_overridden_per_run(budget_h):
    h = budget_h
    before = h.get('admin', '/api/me')
    saved = h.command('admin', 'department.settings', {**department_settings(h),
        'generation_defaults': {'search_minutes': 2, 'max_search_minutes': 7}},
        h.seed['department_id'], before['organisation']['revision'])
    assert saved['department']['generation_defaults']['max_search_minutes'] == 7
    workspace = h.shift(h.workspace(), people=[])['workspace']
    for options in ({'search_minutes': 8}, {'search_seconds': 600}, {'max_search_minutes': 8}):
        rejected = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
            'generation_options': options}, revision=workspace['revision'], status=422)
        assert rejected['error']['code'] in {'SCHEMA_INVALID', 'DEPARTMENT_OPTIONS_ONLY'}
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'generation_options': {'search_minutes': 7}}, revision=workspace['revision'])['job']
    assert job['search_control'] == {'minutes': 7, 'stop': False}
    undo = h.command('admin', 'department.settings', {**saved['previous'], 'reason': 'Undo maximum change'},
        h.seed['department_id'], saved['organisation']['revision'])
    assert undo['department']['generation_defaults']['max_search_minutes'] == 60
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == 'department.settings',
            m.AuditEvent.reason == 'Undo maximum change'))


def test_generation_queue_uses_the_draft_baseline_after_department_defaults_change(budget_h):
    h = budget_h
    workspace = h.shift(h.workspace(), people=[])['workspace']
    with transaction(h.app.state.engine) as db:
        baseline = db.scalar(select(m.WorkspaceBaseline).where(
            m.WorkspaceBaseline.workspace_id == workspace['id']))
        captured = GenerationOptions.model_validate(
            deepcopy(baseline.canonical_payload['departments'][h.seed['department_id']]['generation_defaults'])
        ).model_dump(mode='json')
        assert {'rules', 'score', 'auto'} <= set(baseline.included_sources)

    me = h.get('admin', '/api/me')
    changed = h.command('admin', 'department.settings', {**department_settings(h),
        'generation_defaults': {'search_minutes': 3, 'max_search_minutes': 4}},
        h.seed['department_id'], me['organisation']['revision'])
    assert changed['department']['generation_defaults']['max_search_minutes'] == 4
    assert captured['max_search_minutes'] != 4

    job = h.command('coordinator', 'job.create', {
        'workspace_id': workspace['id'], 'kind': 'generation'}, revision=workspace['revision'])['job']
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.input_snapshot['generation_defaults'] == captured
        assert row.input_snapshot['baseline']['hash'] == baseline.payload_hash
        manifest = row.input_snapshot['generation_manifest']
        assert manifest['baseline']['id'] == baseline.id
        assert manifest['baseline']['hash'] == baseline.payload_hash
        assert manifest['sources']['hashes']['rules']
        assert manifest['generation']['defaults'] == captured
        assert row.read_metadata['input_snapshot']['generation_manifest']['baseline']['hash'] == baseline.payload_hash
        verify_generation_snapshot(db, db.get(m.Workspace, workspace['id']), row.input_snapshot)
        data = domain_snapshot(db, db.get(m.Workspace, workspace['id']),
                               include_fairness=False, include_objectives=False)
        assert GenerationOptions.model_validate(
            data['department']['generation_defaults']).model_dump(mode='json') == captured


def test_department_name_change_cannot_bypass_resolved_search_limit_validation(budget_h):
    h = budget_h
    before = h.get('admin', '/api/me')
    rejected = h.command('admin', 'department.settings', {**department_settings(h),
        'department_name': 'Must not save invalid defaults', 'generation_defaults': {'search_minutes': 100}},
        h.seed['department_id'], before['organisation']['revision'], status=422)
    assert rejected['error']['code'] == 'SCHEMA_INVALID'
    assert h.get('admin', '/api/me')['department'] == before['department']


@pytest.mark.parametrize('role', [role for role in ROLE_NAMES if role not in {'coordinator', 'admin'}])
@pytest.mark.parametrize('kind', ['job.add_time', 'job.stop_keep_best', 'job.continue'])
def test_search_controls_require_start_generation_permission(budget_h, role, kind):
    h = budget_h
    _, job = start(h)
    login_role = role if role in h.seed['identities'] else 'staff'
    if login_role != role:
        with transaction(h.app.state.engine, write=True) as db:
            binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == h.seed['identities'][login_role],
                                                           m.RoleBinding.role == login_role))
            binding.role = role
    reply = h.command(login_role, kind, {'minutes': 5, 'reason': 'A request without authority'} if kind != 'job.stop_keep_best'
                      else {'reason': 'A request without authority'}, job['id'], status=403)
    assert reply['error']['code'] == 'FORBIDDEN'
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job['id']).search_control == {'minutes': 5, 'stop': False, 'spent_seconds': 0}


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_add_time_and_stop_keep_best_are_audited_bounded_and_ignore_heartbeat_revisions(budget_h, status):
    h = budget_h
    _, job = start(h, {'search_minutes': 55})
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.status, row.revision = status, job['revision'] + 4
    extended = h.command('coordinator', 'job.add_time', {'minutes': 5, 'reason': 'Allow one more search interval'},
        job['id'], job['revision'])['job']
    assert extended['search_control'] == {'minutes': 60, 'stop': False}
    rejected = h.command('coordinator', 'job.add_time', {'minutes': 1, 'reason': 'Above department maximum'},
        job['id'], status=422)
    assert rejected['error']['code'] == 'SEARCH_MINUTES_LIMIT'
    stopped = h.command('coordinator', 'job.stop_keep_best', {'reason': 'Keep the current best proposal'},
        job['id'], job['revision'])['job']
    assert stopped['status'] == status
    assert stopped['search_control'] == {'minutes': 60, 'stop': True}
    rejected = h.command('coordinator', 'job.add_time', {'minutes': 1, 'reason': 'Cannot undo a requested stop'},
        job['id'], status=409)
    assert rejected['error']['code'] == 'JOB_STATE'
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.target_id == job['id'])))
    assert {(e.action, e.reason) for e in events} == {
        ('job.add_time', 'Allow one more search interval'), ('job.stop_keep_best', 'Keep the current best proposal')}


@pytest.mark.parametrize('stage', GENERATION_SEARCH_FINISHED_STAGES)
def test_add_time_is_rejected_after_search_has_finished(budget_h, stage):
    h = budget_h
    _, job = start(h)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.status, row.progress = 'running', {'stage': stage}
    rejected = h.command('coordinator', 'job.add_time', {'minutes': 5, 'reason': 'Search has already finished'},
        job['id'], status=409)
    assert rejected['error']['code'] == 'JOB_STATE'
    assert 'Search has finished' in rejected['error']['message']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job['id']).search_control == {'minutes': 5, 'stop': False, 'spent_seconds': 0}
        assert not db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == 'job.add_time',
                                                         m.AuditEvent.target_id == job['id']))


@pytest.mark.parametrize('state', [None, {'model': 'private-prepared-model', 'hint': [0, 1, 1]}])
def test_continue_creates_new_attempt_reuses_frozen_preparation_and_keeps_history_private(budget_h, monkeypatch, state):
    h = budget_h
    workspace, job = start(h)
    checkpoint = exhausted(h, job, state)
    detail = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert detail['outcome'] == 'needs_more_time'
    assert not {'resume_state', 'resume_checkpoint'}.intersection(detail)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        before_snapshot, before_result = deepcopy(row.input_snapshot), deepcopy(row.result)
        assert {'resume_state', 'resume_checkpoint'} <= inspect(row).unloaded
        assert not {'resume_state', 'resume_checkpoint'}.intersection(record(row))
        assert not {'resume_state', 'resume_checkpoint'}.intersection(job_record(row, include_result=False))
        assert {'resume_state', 'resume_checkpoint'} <= inspect(row).unloaded
    monkeypatch.setattr('apps.api.services.jobs.domain_snapshot', lambda *args, **kwargs: pytest.fail('Rebuilt unchanged preparation'))
    continued = h.command('coordinator', 'job.continue', {'minutes': 9, 'reason': 'Continue saved search'}, job['id'])['job']
    assert continued['id'] != job['id']
    assert continued['status'] == 'queued'
    assert continued['effective_options']['search_minutes'] == 9
    assert continued['search_control'] == {'minutes': 9, 'stop': False}
    assert not {'resume_state', 'resume_checkpoint'}.intersection(continued)
    with transaction(h.app.state.engine) as db:
        previous, current = db.get(m.Job, job['id']), db.get(m.Job, continued['id'])
        assert previous.input_snapshot == before_snapshot and previous.result == before_result
        assert current.input_fingerprint == previous.input_fingerprint
        assert current.resume_state == state and previous.resume_state is None
        assert current.resume_checkpoint == checkpoint and previous.resume_checkpoint is None
        assert current.search_control['spent_seconds'] == 0
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == 'job.continue',
            m.AuditEvent.target_id == job['id'], m.AuditEvent.reason == 'Continue saved search'))
    assert len(h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs']) == 2
    latest_rejected = h.command('coordinator', 'job.continue', {'minutes': 5, 'reason': 'A duplicate active attempt'},
        continued['id'], status=409)
    assert latest_rejected['error']['code'] == 'JOB_STATE'
    rejected = h.command('coordinator', 'job.continue', {'minutes': 5, 'reason': 'A duplicate active attempt'},
        job['id'], status=409)
    assert rejected['error']['code'] == 'RUN_SUPERSEDED'


@pytest.mark.parametrize('change,code', [('inputs', None), ('compiler', 'STALE_SCHEDULER'),
                                      ('result_compiler', 'STALE_SCHEDULER'),
                                      ('semantic_defaults_department_only', None),
                                      ('semantic_defaults_after_refresh', 'STALE_INPUTS')])
def test_continue_refuses_changed_model_inputs(budget_h, change, code):
    h = budget_h
    workspace, job = start(h)
    exhausted(h, job, {'model': 'preserved'})
    with transaction(h.app.state.engine, write=True) as db:
        if change == 'inputs':
            db.get(m.Workspace, workspace['id']).content_revision += 1
        elif change == 'compiler':
            row = db.get(m.Job, job['id'])
            row.input_snapshot = {**row.input_snapshot, 'compiler_version': 'old-compiler'}
        elif change == 'result_compiler':
            row = db.get(m.Job, job['id'])
            row.result = {**row.result, 'algorithm_version': 'old-compiler'}
        else:
            department = db.get(m.Department, h.seed['department_id'])
            department.generation_defaults = {'shortlist_width': 5}
    if change == 'semantic_defaults_after_refresh':
        workspace = h.refresh_draft_baseline(workspace)
    reply = h.command('coordinator', 'job.continue', {'minutes': 5, 'reason': 'Continue stale preparation'},
        job['id'], status=409 if code else 200)
    if code:
        assert reply['error']['code'] == code
    else:
        assert reply['job']['input_fingerprint'] == job['input_fingerprint']
        with transaction(h.app.state.engine) as db:
            previous, continued = db.get(m.Job, job['id']), db.get(m.Job, reply['job']['id'])
            assert continued.input_snapshot['generation_defaults'] == previous.input_snapshot['generation_defaults']


def test_increasing_runtime_limit_preserves_preparation_fingerprint_and_continue_uses_new_limit(budget_h):
    h = budget_h
    workspace, job = start(h)
    exhausted(h, job, {'model': 'preserved'})
    me = h.get('admin', '/api/me')
    saved = h.command('admin', 'department.settings', {**department_settings(h),
        'generation_defaults': {'max_search_minutes': 90}}, h.seed['department_id'], me['organisation']['revision'])
    assert saved['organisation']['input_revision'] == me['organisation']['input_revision']
    rejected = h.command('coordinator', 'job.continue', {'minutes': 91, 'reason': 'More than current limit'},
        job['id'], status=422)
    assert rejected['error']['code'] == 'SEARCH_MINUTES_LIMIT'
    continued = h.command('coordinator', 'job.continue', {'minutes': 90, 'reason': 'Use new runtime limit'}, job['id'])['job']
    assert continued['effective_options']['max_search_minutes'] == 90
    assert continued['input_fingerprint'] == job['input_fingerprint']


def test_continue_from_department_defaults_reuses_the_actual_prepared_solver_model(budget_h, monkeypatch):
    from ortools.sat.python import cp_model
    from apps.worker.main import process_one
    from packages.domain import cp_sat
    from packages.domain.solver_control import SolverControl
    from packages.domain.solver_resume import resume_key

    h = budget_h
    _, job = start(h)
    solve = SolverControl.solve
    monkeypatch.setattr(SolverControl, 'solve', lambda *args: cp_model.UNKNOWN)
    assert process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        previous = db.get(m.Job, job['id'])
        assert previous.result['status'] == 'needs_more_time'
        assert previous.input_snapshot['generation_options_overridden'] is False
        saved_key = previous.resume_state['key']
    continued = h.command('coordinator', 'job.continue', {'minutes': 9, 'reason': 'Continue the default search'},
                          job['id'])['job']
    with transaction(h.app.state.engine) as db:
        current = db.get(m.Job, continued['id'])
        assert current.input_snapshot['generation_options_overridden'] is True
        assert saved_key == resume_key(current.input_snapshot,
            GenerationOptions.model_validate(current.input_snapshot['generation_options']), False, False)
    monkeypatch.setattr(SolverControl, 'solve', solve)
    monkeypatch.setattr(cp_sat, '_prepare', lambda *args: pytest.fail('Continue rebuilt the saved solver model'))
    assert process_one(h.app.state.engine)
    result = h.get('coordinator', '/api/jobs/' + continued['id'])['job']['result']
    assert result['status'] == 'candidate_feasible'
    assert result['preparation_reused'] and result['validation']['valid']


def test_continue_rebuilds_preparation_when_a_mandatory_overtime_cap_is_activated(budget_h, monkeypatch):
    from ortools.sat.python import cp_model
    from packages.domain.solver_control import SolverControl
    from packages.domain.solver_resume import resume_key
    from tests.test_overtime_extra_cover import extra_type

    h = budget_h
    duty_type = extra_type(h)
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_overtime'}, {'person_id': person}, severity='advisory')
    draft = h.shift(h.workspace(), typ=duty_type['id'], people=[])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': draft['id'], 'kind': 'generation'},
                    revision=draft['revision'])['job']
    with monkeypatch.context() as interrupted:
        interrupted.setattr(SolverControl, 'solve', lambda *args: cp_model.UNKNOWN)
        assert process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        original = db.get(m.Job, job['id'])
        assert original.result['status'] == 'needs_more_time'
        saved_key = original.resume_state['key']
        saved_fingerprint = original.input_fingerprint
    h.policy({'kind': 'block_quota', 'starts_on': draft['starts_on'], 'ends_on': draft['ends_on'],
              'metric': 'duties', 'comparison': 'maximum', 'amount': 0, 'overtime': 'only'}, {'person_id': person},
             checked=[draft])
    continued = h.command('coordinator', 'job.continue', {'minutes': 1, 'reason': 'Continue prepared search'},
                          job['id'])['job']
    with transaction(h.app.state.engine, write=True) as db:
        resumed = db.get(m.Job, continued['id'])
        # A recovered attempt can have persisted an incumbent under its original fingerprint.
        resumed.result = {'status': 'candidate_feasible', 'input_fingerprint': saved_fingerprint,
                          'checkpointed': True}
    solve = SolverControl.solve

    def verify_reprepared(control, solver, model, tier):
        with transaction(h.app.state.engine) as db:
            running = db.get(m.Job, continued['id'])
            assert running.input_fingerprint != saved_fingerprint
            assert running.result is None
        return solve(control, solver, model, tier)

    monkeypatch.setattr(SolverControl, 'solve', verify_reprepared)
    assert process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        resumed = db.get(m.Job, continued['id'])
        assert resumed.input_fingerprint != saved_fingerprint
        assert resumed.input_fingerprint == resumed.input_snapshot['input_manifest']['token']
        options = GenerationOptions.model_validate(resumed.input_snapshot['generation_options'])
        assert resume_key(resumed.input_snapshot, options, False, False) != saved_key
        assert resumed.result['status'] == 'candidate_feasible'
        assert resumed.result['additions'] == [] and resumed.result['validation']['valid']
        assert not resumed.result['preparation_reused']
        assert db.get(m.Job, job['id']).input_fingerprint == saved_fingerprint


def test_new_run_and_continue_have_one_cache_owner_even_with_a_fixed_clock(budget_h, monkeypatch):
    h = budget_h
    fixed = m.now()
    freeze_clock(monkeypatch, lambda: fixed)
    workspace, original = start(h)
    exhausted(h, original, {'encoding': 'gzip', 'data': 'opaque compressed model', 'key': 'saved'})
    replacement = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
                            revision=workspace['revision'])['job']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, original['id']).resume_state is None
        assert db.get(m.Job, original['id']).resume_checkpoint is None
    exhausted(h, replacement, {'encoding': 'gzip', 'data': 'latest model', 'key': 'latest'})
    rejected = h.command('coordinator', 'job.continue', {'minutes': 3, 'reason': 'Cannot take the newer cache'},
                         original['id'], status=409)
    assert rejected['error']['code'] == 'RUN_SUPERSEDED'
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, replacement['id'])
        row.search_control = {'minutes': 5, 'stop': False, 'spent_seconds': 280}
    continued = h.command('coordinator', 'job.continue', {'minutes': 3, 'reason': 'Move latest preparation'}, replacement['id'])['job']
    with transaction(h.app.state.engine) as db:
        old, current = db.get(m.Job, replacement['id']), db.get(m.Job, continued['id'])
        assert old.resume_state is None and old.resume_checkpoint is None
        assert current.resume_state == {'encoding': 'gzip', 'data': 'latest model', 'key': 'latest'}
        assert current.resume_checkpoint and current.search_control['spent_seconds'] == 0
        assert current.created_at > old.created_at
        owners = list(db.scalars(select(m.Job.id).where(m.Job.workspace_id == workspace['id'],
            m.Job.resume_state.is_not(None))))
        assert owners == [current.id]
        assert db.scalar(select(m.Job.id).where(m.Job.id == old.id,
            m.Job.resume_state.is_(None), m.Job.resume_checkpoint.is_(None))) == old.id


@pytest.mark.parametrize('mutation', ['content', 'settings', 'bulk', 'archive', 'cancel'])
def test_canonical_mutations_preserve_frozen_preparation_until_authority_ends(budget_h, mutation):
    from types import SimpleNamespace
    from apps.api.services.bulk import _batch
    from apps.api.services.common import bump_inputs

    h = budget_h
    workspace, job = start(h)
    exhausted(h, job, {'model': 'disposable prepared model'})
    with transaction(h.app.state.engine) as db:
        evidence = deepcopy(db.get(m.Job, job['id']).result)
    if mutation == 'content':
        h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])
    elif mutation == 'settings':
        me = h.get('admin', '/api/me')
        h.command('admin', 'department.settings', {**department_settings(h), 'generation_defaults': {'shortlist_width': 5}},
                  h.seed['department_id'], me['organisation']['revision'])
    elif mutation == 'bulk':
        with transaction(h.app.state.engine, write=True) as db:
            actor = SimpleNamespace(organisation_id=h.seed['organisation_id'], department_id=h.seed['department_id'])
            with _batch(db, actor, 'synthetic-invalidation'):
                bump_inputs(db, actor)
    elif mutation == 'archive':
        h.command('coordinator', 'workspace.archive', {'reason': 'Discard the draft'}, workspace['id'], workspace['revision'])
    else:
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Job, job['id']).status = 'running'
        h.command('coordinator', 'job.cancel', {'reason': 'Cancel this attempt'}, job['id'])
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        if mutation in {'archive', 'cancel'}:
            assert row.resume_state is None and row.resume_checkpoint is None
        else:
            assert row.resume_state == {'model': 'disposable prepared model'} and row.resume_checkpoint
        assert row.result == evidence


def test_expired_continue_keeps_inputs_but_does_not_retain_old_model_or_hint(budget_h):
    h = budget_h
    _, job = start(h)
    exhausted(h, job, {'encoding': 'gzip', 'data': 'expired model', 'key': 'expired'})
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.completed_at = m.now() - timedelta(days=h.settings.generation_resume_days + 1)
        snapshot, result = deepcopy(row.input_snapshot), deepcopy(row.result)
    reply = h.command('coordinator', 'job.continue', {'minutes': 5, 'reason': 'Rebuild saved inputs after cache expiry'}, job['id'])
    assert reply['preparation_expired'] is True
    with transaction(h.app.state.engine) as db:
        previous, current = db.get(m.Job, job['id']), db.get(m.Job, reply['job']['id'])
        assert previous.input_snapshot == snapshot and previous.result == result
        assert previous.resume_state is None and previous.resume_checkpoint is None
        assert current.resume_state is None and current.resume_checkpoint is None
        assert current.input_fingerprint == previous.input_fingerprint
        assert set(db.scalars(select(m.Job.id).where(m.Job.id.in_([previous.id, current.id]),
            m.Job.resume_state.is_(None), m.Job.resume_checkpoint.is_(None)))) == {previous.id, current.id}


def test_resume_retention_setting_is_configurable_and_validated(monkeypatch):
    from apps.api.settings import Settings
    monkeypatch.setenv('ROTA_GENERATION_RESUME_DAYS', '14')
    assert Settings.from_env().generation_resume_days == 14
    with pytest.raises(RuntimeError, match='ROTA_GENERATION_RESUME_DAYS'):
        Settings(environment='test', generation_resume_days=0).check(require_authentication=False)


def test_manual_check_keeps_resumable_preparation_and_draft_revision(budget_h):
    h = budget_h
    workspace, job = start(h)
    state = {'encoding': 'gzip', 'data': 'private prepared model', 'key': 'unchanged inputs'}
    checkpoint = exhausted(h, job, state)
    checked = h.lifecycle(workspace, 'validate')['workspace']
    assert checked['revision'] == workspace['revision']
    assert checked['content_revision'] == workspace['content_revision']
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.resume_state == state and row.resume_checkpoint == checkpoint
    continued = h.command('coordinator', 'job.continue', {'minutes': 3,
        'reason': 'Continue prepared search after checking the draft'}, job['id'])['job']
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, continued['id'])
        assert row.resume_state == state and row.resume_checkpoint == checkpoint


def test_submit_clears_preparation_without_any_staff_wishes(budget_h):
    h = budget_h
    workspace = h.shift(h.workspace())['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
                    revision=workspace['revision'])['job']
    exhausted(h, job, {'model': 'old draft preparation'})
    h.lifecycle(workspace, 'submit')
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job['id']).resume_state is None
        assert db.get(m.Job, job['id']).resume_checkpoint is None


def test_completed_proposal_and_apply_keep_no_private_cache(budget_h):
    from apps.worker.main import process_one
    h = budget_h
    workspace, job = start(h)
    assert process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.result['status'] == 'candidate_feasible'
        assert row.resume_state is None and row.resume_checkpoint is None
    h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'], 'job_id': job['id'],
        'reason': 'Apply reviewed proposal'}, revision=workspace['revision'])
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.status == 'applied' and row.resume_state is None and row.resume_checkpoint is None


@pytest.mark.postgres
def test_postgres_resume_move_and_retention_preserve_evidence_and_use_sql_null(pg):
    from apps.api.services.generation_resume import prune_generation_resume
    h = pg
    h.app.state.engine.dispose()
    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings)
    workspace, original = start(h)
    state = {'encoding': 'gzip', 'data': 'opaque prepared model', 'key': 'latest'}
    exhausted(h, original, state)
    current = h.command('coordinator', 'job.continue', {'minutes': 3, 'reason': 'Move prepared PostgreSQL state'},
                        original['id'])['job']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, current['id']).resume_state == state
        assert db.scalar(select(m.Job.id).where(m.Job.id == original['id'],
            m.Job.resume_state.is_(None), m.Job.resume_checkpoint.is_(None))) == original['id']
        assert db.scalar(select(func.count()).select_from(m.Job).where(m.Job.workspace_id == workspace['id'], or_(
            m.Job.resume_state.is_not(None), m.Job.resume_checkpoint.is_not(None)))) == 1
    # Exercise the correlated superseded predicate against an old pre-upgrade duplicate.
    exhausted(h, original, {'model': 'old duplicate'})
    with transaction(h.app.state.engine, write=True) as db:
        assert prune_generation_resume(db, days=7) == 1
    exhausted(h, current, state)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, current['id'])
        row.completed_at = m.now() - timedelta(days=8)
        snapshot, result = deepcopy(row.input_snapshot), deepcopy(row.result)
        assert prune_generation_resume(db, days=7) == 1
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, current['id'])
        assert row.input_snapshot == snapshot and row.result == result
        assert db.scalar(select(m.Job.id).where(m.Job.id == current['id'],
            m.Job.resume_state.is_(None), m.Job.resume_checkpoint.is_(None))) == current['id']
    reply = h.command('coordinator', 'job.continue', {'minutes': 2, 'reason': 'Rebuild retained inputs after expiry'}, current['id'])
    assert reply['preparation_expired']
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Job.id).where(m.Job.id == reply['job']['id'],
            m.Job.resume_state.is_(None), m.Job.resume_checkpoint.is_(None))) == reply['job']['id']


@pytest.mark.postgres
def test_postgres_prune_rechecks_eligibility_after_a_selected_job_turns_running(pg):
    from apps.api.services.generation_resume import prune_generation_resume

    engine = pg.app.state.engine
    workspace = pg.workspace()
    checkpoint = {'hint': [1], 'strict_conflict': {'strict_status': 'FEASIBLE'}}
    with transaction(engine, write=True) as db:
        row = m.Job(department_id=pg.seed['department_id'], workspace_id=workspace['id'],
                    requested_by=pg.seed['identities']['coordinator'], kind='generation', status='succeeded',
                    input_fingerprint='prune-race', input_snapshot={}, result={'status': 'failed'},
                    resume_state={'prepared': True}, resume_checkpoint=checkpoint)
        db.add(row)
        db.flush()
        job_id = row.id

    holder = engine.connect()
    held = holder.begin()
    holder.execute(select(m.Job.__table__.c.id).where(m.Job.__table__.c.id == job_id).with_for_update())
    started, finished = Event(), Event()
    result = {}
    waiter_pid = []

    def capture(connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith('UPDATE JOBS') and 'resume_state' in statement:
            waiter_pid.append(connection.connection.driver_connection.info.backend_pid)
            started.set()

    def prune():
        try:
            with transaction(engine, write=True) as db:
                result['count'] = prune_generation_resume(db, days=7, limit=1)
        except BaseException as exc:  # surfaced by the assertion below
            result['error'] = exc
        finally:
            finished.set()

    event.listen(engine, 'before_cursor_execute', capture)
    worker = Thread(target=prune)
    try:
        worker.start()
        assert started.wait(5), 'prune did not issue its bounded update'
        deadline = monotonic() + 5
        waiting = False
        with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as probe:
            while monotonic() < deadline and not finished.is_set():
                waiting = probe.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE pid = :pid AND wait_event_type = 'Lock')"),
                    {'pid': waiter_pid[0]})
                if waiting:
                    break
                finished.wait(.01)
        assert waiting, 'prune update did not wait on the selected row'
        holder.execute(update(m.Job.__table__).where(m.Job.__table__.c.id == job_id).values(status='running'))
        held.commit()
        worker.join(5)
        assert not worker.is_alive(), 'prune worker did not finish after the row lock released'
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
        if held.is_active:
            held.rollback()
        holder.close()

    assert 'error' not in result, result.get('error')
    assert result['count'] == 0
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'running'
        assert row.resume_state == {'prepared': True} and row.resume_checkpoint == checkpoint
