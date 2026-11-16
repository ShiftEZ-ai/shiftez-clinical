"""Catalogue proof keeps Jobs durability while checking the whole frozen department."""
import copy
import threading
from datetime import timedelta

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import retry_write, transaction
from apps.api.errors import Problem
from apps.api.services import jobs, rule_catalogue_v2
from apps.worker.catalogue_proof import run
from apps.worker.main import claim_job, finish_job, heartbeat_job, process_one
from packages.domain.solver_control import Cancelled
from packages.domain import cp_sat, rule_migration_proof


def queue(h, role='coordinator'):
    client = h.client(role)
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        candidate = rule_catalogue_v2.plan(db, actor, proof=False)
        return jobs.enqueue_catalogue_proof(db, actor, candidate)


def fixture(h):
    h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    return h.workspace('Catalogue proof anchor')


def stable_identity(monkeypatch):
    # Isolate lifecycle tests from deliberate replacements of proof functions below.
    monkeypatch.setattr(jobs, 'catalogue_proof_identity', lambda: {'test_build': 'frozen'})


def test_full_department_proof_is_durable_deduplicated_and_nonmutating(h, monkeypatch):
    from apps.worker import main as worker
    idle_limits = []
    original = worker.allow_long_idle
    def guarded(db, minutes):
        assert not db.in_transaction()
        idle_limits.append(minutes)
        original(db, minutes)
    monkeypatch.setattr(worker, 'allow_long_idle', guarded)
    first = fixture(h)
    h.shift(first)
    rotas = [first, h.workspace('Submitted rota'), h.workspace('Approved rota')]
    ignored = h.workspace('Historical rota')
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, rotas[1]['id']).status = 'submitted'
        db.get(m.Workspace, rotas[2]['id']).status = 'approved'
        db.get(m.Workspace, ignored['id']).status = 'superseded'
    before = [h.snapshot(workspace) for workspace in rotas]
    queued = queue(h, 'approver')
    assert queued['kind'] == 'catalogue_proof' and queued['status'] == 'queued'
    assert queue(h)['id'] == queued['id']
    with transaction(h.app.state.engine) as db:
        frozen = db.get(m.Job, queued['id']).input_snapshot
        assert frozen['rota_ids'] == sorted(workspace['id'] for workspace in rotas)
        assert 'shifts' not in frozen and 'baseline' not in frozen
    assert process_one(h.app.state.engine)
    assert idle_limits == [30]
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, queued['id'])
        result = copy.deepcopy(row.result)
        assert row.status == 'succeeded' and row.progress['stage'] == 'ready'
        assert result['status'] == 'identical' and result['proof']['identical']
        assert [rota['workspace_id'] for rota in result['proof']['rotas']] == frozen['rota_ids']
        assert all(rota['model']['equal'] and rota['score']['equal'] for rota in result['proof']['rotas'])
    assert [h.snapshot(workspace) for workspace in rotas] == before
    assert queue(h)['id'] == queued['id']


def test_queue_does_not_load_rota_snapshots(h, monkeypatch):
    fixture(h)
    for index in range(14):
        h.workspace(f'Current rota {index}')
    stable_identity(monkeypatch)
    def no_snapshot(*args, **kwargs):
        raise AssertionError('Queueing must not load fifteen rota snapshots')
    monkeypatch.setattr(rule_catalogue_v2, 'domain_snapshot', no_snapshot)
    queued = queue(h)
    with transaction(h.app.state.engine) as db:
        assert len(db.get(m.Job, queued['id']).input_snapshot['rota_ids']) == 15


def test_background_preview_is_readonly_and_check_is_audited_idempotent_and_role_scoped(h, monkeypatch):
    fixture(h)
    stable_identity(monkeypatch)
    endpoint = '/api/rules/catalogue/migration?version=2&proof=true&background=true'
    preview = h.get('coordinator', endpoint)
    assert preview['proof_state'] == 'not_checked' and preview['proof'] is None and preview['proof_job'] is None
    with transaction(h.app.state.engine) as db:
        assert not list(db.scalars(select(m.Job)))
    payload = {'version': 2, 'plan_hash': preview['plan_hash'], 'reason': 'Check full department preservation'}
    checked = h.command('approver', 'rule.catalogue.check', payload, key='catalogue-proof-idempotent')
    assert h.command('approver', 'rule.catalogue.check', payload, key='catalogue-proof-idempotent') == checked
    assert h.command('coordinator', 'rule.catalogue.check', payload)['job']['id'] == checked['job']['id']
    job = checked['job']
    queued = h.get('coordinator', endpoint)
    assert queued['proof_state'] == 'queued' and queued['proof_job']['id'] == job['id']
    assert h.get('approver', '/api/jobs/' + job['id'])['job']['check_stale'] is False
    assert any(row['id'] == job['id'] for row in h.get('approver', '/api/jobs')['jobs'])
    h.get('staff', '/api/jobs/' + job['id'], status=403)
    h.command('auditor', 'rule.catalogue.check', payload, status=403)
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.Job)))) == 1
        assert len(list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'rule.catalogue.check')))) == 2
    assert process_one(h.app.state.engine)
    completed = h.get('coordinator', endpoint)
    assert completed['proof_state'] == 'passed' and completed['proof']['identical']
    full = h.get('approver', '/api/jobs/' + job['id'])['job']['result']['proof']
    assert all(rota['model']['before']['tiers'] and rota['model']['after']['tiers'] for rota in full['rotas'])
    # A fresh exact receipt supplies propose/activate; neither request recompiles every current rota.
    def no_inline_proof(*args, **kwargs):
        raise AssertionError('A fresh durable receipt must avoid inline recompilation')
    monkeypatch.setattr(rule_catalogue_v2, '_proof', no_inline_proof)
    conversion = h.command('coordinator', 'rule.migrate_to_catalogue', payload)['record']
    assert conversion['proof'] == rule_catalogue_v2._preview_proof(full)
    assert h.get('auditor', '/api/rules/catalogue/conversions/' + conversion['id'] + '?details=true')['record']['proof'] == full
    activated = h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent full proof review'},
        conversion['id'], conversion['revision'])['record']
    assert activated['status'] == 'active' and activated['proof'] == rule_catalogue_v2._preview_proof(full)
    assert h.get('auditor', '/api/rules/catalogue/conversions/' + conversion['id'] + '?details=true')['record']['proof'] == full


def test_background_preview_compacts_model_capture_without_changing_full_receipt(h):
    workspace = fixture(h)
    h.shift(workspace)
    h.workspace('Second current rota')
    queued = queue(h)
    assert process_one(h.app.state.engine)
    full = h.get('coordinator', '/api/jobs/' + queued['id'])['job']['result']['proof']
    preview = h.get('coordinator', '/api/rules/catalogue/migration?version=2&proof=true&background=true')
    assert preview['proof_state'] == 'passed' and preview['proof_job']['id'] == queued['id']
    compact = copy.deepcopy(preview['proof'])
    assert len(compact['rotas']) == len(full['rotas']) == 2
    for shown, saved in zip(compact['rotas'], full['rotas']):
        for side in ('before', 'after'):
            assert 'tiers' not in shown['model'][side]
            assert saved['model'][side]['tiers']
            shown['model'][side]['tiers'] = saved['model'][side]['tiers']
    # Every other result, readiness flag, count and digest survives the response projection.
    assert compact == full
    assert h.get('coordinator', '/api/rules/catalogue/migration?version=2&proof=true&details=true')['proof'] == full
    assert h.get('coordinator', '/api/jobs/' + queued['id'])['job']['result']['proof'] == full


def test_department_proof_anchor_does_not_replace_workspace_generation(h, monkeypatch):
    workspace = fixture(h)
    stable_identity(monkeypatch)
    proof = queue(h)
    generation = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
                           revision=workspace['revision'])['job']
    listed = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs']
    assert [job['id'] for job in listed] == [generation['id']]
    assert {job['id'] for job in h.get('coordinator', '/api/jobs')['jobs']} == {proof['id'], generation['id']}
    assert h.get('coordinator', '/api/jobs/' + proof['id'])['job']['id'] == proof['id']


@pytest.mark.parametrize('kind', ['generation', 'feasibility', 'catalogue_proof'])
def test_catalogue_proof_shares_one_heavy_slot_and_recovers_its_lease(h, monkeypatch, kind):
    workspace = fixture(h)
    stable_identity(monkeypatch)
    queued = queue(h)
    claim = retry_write(h.app.state.engine, claim_job)
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Job(department_id=h.seed['department_id'], workspace_id=workspace['id'],
            requested_by=h.seed['identities']['coordinator'], kind=kind,
            input_fingerprint='test', input_snapshot={}))
        row = db.get(m.Job, queued['id'])
        row.started_at = m.now() - timedelta(hours=1)
    assert retry_write(h.app.state.engine, lambda db: heartbeat_job(db, claim, {'stage': 'proving'}))
    assert retry_write(h.app.state.engine, claim_job) is None
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Job, queued['id']).heartbeat_at = m.now() - timedelta(hours=1)
    recovered = retry_write(h.app.state.engine, claim_job)
    assert recovered['id'] == claim['id'] and recovered['attempt'] == claim['attempt'] + 1
    retry_write(h.app.state.engine, lambda db: finish_job(db, claim, {'status': 'identical'}, None))
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, queued['id']).status == 'running'


@pytest.mark.parametrize('queued', [True, False])
def test_cancelled_proof_never_admits_a_late_result(h, monkeypatch, queued):
    fixture(h)
    stable_identity(monkeypatch)
    job = queue(h)
    claim = None if queued else retry_write(h.app.state.engine, claim_job)
    h.command('operator', 'job.cancel', {'reason': 'Stop conversion proof'}, job['id'], job['revision'])
    if claim:
        retry_write(h.app.state.engine, lambda db: finish_job(db, claim, {'status': 'identical'}, None))
    else:
        assert retry_write(h.app.state.engine, claim_job) is None
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.status == 'cancelled' and row.result is None


@pytest.mark.parametrize('before_claim', [True, False])
def test_proof_reauthorizes_approver_at_claim_and_completion(h, monkeypatch, before_claim):
    fixture(h)
    stable_identity(monkeypatch)
    job = queue(h, 'approver')
    claim = None if before_claim else retry_write(h.app.state.engine, claim_job)
    with transaction(h.app.state.engine, write=True) as db:
        for binding in db.scalars(select(m.RoleBinding).where(
                m.RoleBinding.identity_id == h.seed['identities']['approver'])):
            binding.revoked = True
    if before_claim:
        assert retry_write(h.app.state.engine, claim_job) is None
    else:
        assert not retry_write(h.app.state.engine, lambda db: heartbeat_job(db, claim, {}))
        retry_write(h.app.state.engine, lambda db: finish_job(db, claim, {'status': 'identical'}, None))
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.status == 'failed' and row.error_code == 'ACCESS_REVOKED' and row.result is None


@pytest.mark.parametrize('when', ['queued', 'during', 'before_finish'])
def test_changed_department_never_admits_proof(h, monkeypatch, when):
    fixture(h)
    stable_identity(monkeypatch)
    job = queue(h)
    if when == 'queued':
        h.workspace('Changed after queue')
        assert process_one(h.app.state.engine)
    elif when == 'before_finish':
        claim = retry_write(h.app.state.engine, claim_job)
        h.workspace('Changed at final commit boundary')
        retry_write(h.app.state.engine, lambda db: finish_job(db, claim, {'status': 'identical'}, None))
    else:
        original = rule_catalogue_v2._proof
        checked = []
        def changing(*args, **kwargs):
            proof = original(*args, **kwargs)
            checked.append(proof)
            h.workspace('Changed during proof')
            return proof
        monkeypatch.setattr(rule_catalogue_v2, '_proof', changing)
        assert process_one(h.app.state.engine)
        assert checked and checked[0]['identical']
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.status == 'failed' and row.error_code == 'STALE_INPUTS' and row.result is None


def test_worker_cancellation_checkpoint_and_build_change_fail_closed(h, monkeypatch):
    fixture(h)
    stable_identity(monkeypatch)
    job = queue(h)
    claim = retry_write(h.app.state.engine, claim_job)
    with transaction(h.app.state.engine) as db, pytest.raises(Cancelled):
        run(db, claim, progress=lambda _: None, cancelled=lambda: True)
    monkeypatch.setattr(jobs, 'catalogue_proof_identity', lambda: {'test_build': 'different'})
    retry_write(h.app.state.engine, lambda db: finish_job(db, claim, {'status': 'identical'}, None))
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.status == 'failed' and row.error_code == 'STALE_INPUTS' and row.result is None


def test_unverifiable_build_cannot_queue_or_admit_proof(h, monkeypatch):
    fixture(h)
    stable_identity(monkeypatch)
    job = queue(h)
    claim = retry_write(h.app.state.engine, claim_job)
    monkeypatch.setattr(jobs, 'catalogue_proof_identity', lambda: None)
    with pytest.raises(Problem) as error:
        queue(h)
    assert error.value.code == 'BUILD_IDENTITY_REQUIRED'
    retry_write(h.app.state.engine, lambda db: finish_job(db, claim, {'status': 'identical'}, None))
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.status == 'failed' and row.result is None


def test_frozen_candidate_changes_fail_before_proof(h, monkeypatch):
    fixture(h)
    stable_identity(monkeypatch)
    job = queue(h)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        snapshot = copy.deepcopy(row.input_snapshot)
        snapshot['candidate']['targets'][0]['payload']['definition']['rule']['lines'][0]['numbers']['max']['value'] = 100
        row.input_snapshot = snapshot
    assert process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.status == 'failed' and row.error_code == 'STALE_INPUTS' and row.result is None


def test_completed_nonidentical_proof_cannot_propose_conversion(h, monkeypatch):
    fixture(h)
    stable_identity(monkeypatch)
    monkeypatch.setattr(rule_catalogue_v2, '_proof', lambda *args, **kwargs: {'identical': False, 'rotas': []})
    job = queue(h)
    assert process_one(h.app.state.engine)
    preview = h.get('coordinator', '/api/rules/catalogue/migration?version=2&background=true')
    assert preview['proof_state'] == 'different' and preview['proof_job']['id'] == job['id']
    refused = h.command('coordinator', 'rule.migrate_to_catalogue', {'version': 2,
        'plan_hash': preview['plan_hash'], 'reason': 'Different behaviour cannot be admitted'}, status=409)
    assert refused['error']['code'] == 'EQUIVALENCE_FAILED'


@pytest.mark.parametrize('compile_index', [1, 2])
def test_strict_proof_cancellation_reaches_each_production_compiler(h, monkeypatch, compile_index):
    data = h.snapshot(h.shift(h.workspace())['workspace'])
    stopped, calls = threading.Event(), []
    original = cp_sat.canonical_order
    def stop_during_preparation(snapshot):
        calls.append(snapshot)
        if len(calls) == compile_index:
            stopped.set()
        return original(snapshot)
    monkeypatch.setattr(cp_sat, 'canonical_order', stop_during_preparation)
    with pytest.raises(Cancelled):
        rule_migration_proof.strict_proof(data, copy.deepcopy(data), cancelled=stopped.is_set)
    assert len(calls) == compile_index


@pytest.mark.parametrize('phase', ['evaluate', 'schedule_score'])
def test_strict_proof_cancels_between_validation_and_score_phases(h, monkeypatch, phase):
    data = h.snapshot(h.shift(h.workspace())['workspace'])
    stopped, calls = threading.Event(), []
    original = getattr(rule_migration_proof, phase)
    def stop_after_phase(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        stopped.set()
        return result
    monkeypatch.setattr(rule_migration_proof, phase, stop_after_phase)
    with pytest.raises(Cancelled):
        rule_migration_proof.strict_proof(data, copy.deepcopy(data), cancelled=stopped.is_set)
    assert len(calls) == 1


def test_objective_capture_cancels_between_bound_probes(h, monkeypatch):
    data = h.snapshot(h.shift(h.workspace())['workspace'])
    stopped, calls = threading.Event(), []
    original = cp_sat.objective_bound
    def stop_after_probe(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        stopped.set()
        return result
    monkeypatch.setattr(cp_sat, 'objective_bound', stop_after_probe)
    with pytest.raises(Cancelled):
        rule_migration_proof.canonical_full_model(data, cancelled=stopped.is_set)
    assert len(calls) == 1
