"""Dark launch: existing departments stay unchanged until an audited opt-in."""
import copy
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from apps.api import models as m, snapshots
from apps.api.db import transaction
from apps.api.main import create_app
from apps.api.services import score_check_provider as provider
from apps.api.settings import quota_policy_limit
from apps.worker import score_check as worker
from apps.worker.main import claim_job
from packages.domain.validation import ENGINE_VERSION


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def department(request):
    h = request.getfixturevalue(request.param)
    h.command('admin', 'role.grant', {'identity_id': h.seed['identities']['operator'],
        'role': 'admin', 'starts_on': '2020-01-01', 'reason': 'Owner manages department settings'})
    return h


def setting(h, enabled, *, role='operator', status=200):
    me = h.get('admin', '/api/me')
    dept, org = me['department'], me['organisation']
    return h.command(role, 'department.settings', {
        'department_name': dept['name'], 'department_revision': dept['revision'],
        'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
        'score_check_enabled': enabled, 'reason': 'Synthetic feature qualification'},
        dept['id'], org['revision'], status=status)


def forbid_outbound(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Disabled score checking attempted media preparation or an outbound request')
    for name in ('extract', 'test_connection', 'prepare_file', '_post'):
        monkeypatch.setattr(provider, name, forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbidden)


def test_default_off_refuses_every_feature_endpoint_and_schedules_nothing(department, monkeypatch):
    h = department
    assert h.get('admin', '/api/me')['department']['score_check_enabled'] is False
    forbid_outbound(monkeypatch)
    for path in ('/api/score-imports/providers', '/api/score-imports/missing',
                 '/api/score-imports/missing/check', '/api/score-imports/missing/matches',
                 '/api/benchmark?workspace=missing', '/api/benchmarks', '/api/benchmarks/missing',
                 '/api/score-checks', '/api/score-checks/missing', '/api/ai-providers',
                 '/api/ai-providers/calls', '/api/score-extraction-jobs/missing'):
        assert h.get('coordinator', path, status=404)['error']['code'] == 'SCORE_CHECK_DISABLED'
    client = h.client('coordinator')
    for path in ('/api/score-imports', '/api/score-imports/missing/sources'):
        response = client.post(path, files={'files': ('rota.csv', b'Person,Date,Code\n', 'text/csv')},
                               headers={'idempotency-key': path})
        assert response.status_code == 404 and response.json()['error']['code'] == 'SCORE_CHECK_DISABLED'
    for kind, role in (('score_check.auto', 'coordinator'), ('score_check.upload', 'coordinator'),
                       ('score_import.decide', 'coordinator'), ('ai_provider.save', 'operator'),
                       ('ai_provider.test', 'operator')):
        if kind == 'score_check.upload':
            # JSON file uploads are already refused independently of feature availability.
            assert h.command(role, kind, status=422)['error']['code'] == 'UPLOAD_MULTIPART'
        else:
            assert h.command(role, kind, status=404)['error']['code'] == 'SCORE_CHECK_DISABLED'
    assert worker.process_one(h.app.state.engine, h.settings) is False
    with transaction(h.app.state.engine) as db:
        for model in (m.Workspace, m.Job, m.ScoreCheckImportBatch, m.ScoreCheckExtractionJob, m.AiCall):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_setting_is_strict_authorized_audited_undoable_and_preserved_when_omitted(department):
    h = department
    setting(h, True, role='coordinator', status=403)
    assert setting(h, True, role='admin', status=403)['error']['code'] == 'FORBIDDEN'
    assert h.get('admin', '/api/me')['department']['score_check_enabled'] is False
    setting(h, 'true', status=422)
    saved = setting(h, True)
    assert saved['department']['score_check_enabled'] is True
    assert saved['previous']['score_check_enabled'] is False
    me = h.get('admin', '/api/me')
    kept = h.command('admin', 'department.settings', {
        **{k: v for k, v in saved['previous'].items() if k != 'score_check_enabled'},
        'reason': 'Older clients preserve feature settings'}, me['department']['id'], me['organisation']['revision'])
    assert kept['department']['score_check_enabled'] is True
    undone = h.command('admin', 'department.settings', {**saved['previous'],
        'department_revision': kept['department']['revision'], 'reason': 'Undo'},
        kept['department']['id'], kept['organisation']['revision'])
    assert undone['department']['score_check_enabled'] is False
    assert h.command('admin', 'department.settings', {**kept['previous'],
        'department_revision': undone['department']['revision'], 'reason': 'Undo disable requires owner'},
        undone['department']['id'], undone['organisation']['revision'], status=403)['error']['code'] == 'FORBIDDEN'
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.AuditEvent).where(
            m.AuditEvent.action == 'department.settings')) == 3
    assert h.get('coordinator', '/api/score-checks', status=404)['error']['code'] == 'SCORE_CHECK_DISABLED'


@pytest.mark.parametrize('role', ['admin', 'coordinator', 'staff'])
def test_refused_enable_attempt_has_durable_scoped_audit_without_a_settings_change(department, role):
    h = department
    before = h.get('admin', '/api/me')['department']
    assert setting(h, True, role=role, status=403)['error']['code'] == 'FORBIDDEN'
    after = h.get('admin', '/api/me')['department']
    assert after == before
    with transaction(h.app.state.engine) as db:
        event = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'department.score_check_refused')).one()
        assert event.department_id == event.target_id == before['id']
        assert event.actor_id == h.seed['identities'][role] and event.correlation_id
        assert event.detail == {'error_code': 'FORBIDDEN'}
        assert event.reason == 'Score-check enablement refused'
        assert db.scalar(select(func.count()).select_from(m.CommandReceipt).where(
            m.CommandReceipt.command_kind == 'department.settings')) == 0
    audit = h.get('auditor', '/api/audit?action=department')
    assert [event['action'] for event in audit['events']] == ['department.score_check_refused']


@pytest.mark.parametrize('state', ['queued', 'running'])
def test_disabling_blocks_replays_and_pauses_queued_ai_and_benchmark_jobs(department, monkeypatch, state):
    from .test_score_check_ai import queued_file
    from .test_score_check_benchmark import imported
    h = department
    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings, engine=h.app.state.engine)
    setting(h, True)
    _profile, _batch, _source, extraction = queued_file(h)
    source = imported(h)
    payload = {'workspace_id': source['id']}
    auto = h.command('coordinator', 'score_check.auto', payload, revision=source['revision'], key='retained-auto-request')
    if state == 'running':
        with transaction(h.app.state.engine, write=True) as db:
            job = db.get(m.Job, auto['job']['id'])
            job.status, job.started_at = state, m.now() - timedelta(days=1)
            extraction_job = db.get(m.ScoreCheckExtractionJob, extraction)
            extraction_job.state, extraction_job.lease_expires_at = state, m.now() - timedelta(days=1)
    setting(h, False)
    forbid_outbound(monkeypatch)
    refused = h.command('coordinator', 'score_check.auto', payload,
                        revision=source['revision'], key='retained-auto-request', status=404)
    assert refused['error']['code'] == 'SCORE_CHECK_DISABLED'
    assert worker.process_one(h.app.state.engine, h.settings) is False
    with transaction(h.app.state.engine, write=True) as db:
        assert claim_job(db) is None
        assert db.get(m.Job, auto['job']['id']).status == state
        assert db.get(m.ScoreCheckExtractionJob, extraction).state == state
        assert db.scalar(select(func.count()).select_from(m.AiCall)) == 0
    # Existing Planner work can still run while a feature job is paused.
    ordinary = h.shift(h.workspace('Ordinary planner'))['workspace']
    normal = h.command('coordinator', 'job.create', {'workspace_id': ordinary['id'], 'kind': 'generation'},
                       revision=ordinary['revision'])
    with transaction(h.app.state.engine, write=True) as db:
        assert claim_job(db)['id'] == normal['job']['id']


def test_off_preserves_ordinary_amendment_fingerprint_scoring_and_publication(department):
    from apps.api.services import planning
    h = department
    published = h.publish(h.shift(h.workspace('Ordinary publication'))['workspace'])['publication_id']
    amendment = h.workspace('Ordinary amendment', base=published)
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, amendment['id'])
        department = db.get(m.Department, workspace.department_id)
        organisation = db.get(m.Organisation, department.organisation_id)
        legacy = snapshots.snapshot_hash({'engine_version': ENGINE_VERSION, 'quota_policy_limit': quota_policy_limit(),
            'workspace_id': workspace.id, 'content_revision': workspace.content_revision,
            'input_revision': organisation.input_revision, 'availability_revision': organisation.availability_revision,
            'base_publication_id': workspace.base_publication_id})
        data = snapshots.domain_snapshot(db, workspace)
        assert snapshots.fingerprint(db, workspace) == data['input_manifest']['token'] != legacy
        assert 'score_baseline' not in data
        checked = planning.revalidate(db, workspace, store=False)
        # D11 retains issued checks: a scoped amendment cannot claim a current whole-rota score.
        assert checked['scope'] == 'published_changes' and 'score' not in checked
        before = copy.deepcopy(db.get(m.Publication, published).snapshot)
    successor = h.publish(amendment)['publication_id']
    assert successor != published
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Publication, published).snapshot == before
        checked = db.get(m.Publication, successor).validation
        assert checked['scope'] == 'published_changes' and 'score' not in checked


@pytest.mark.parametrize('amendment', [False, True])
def test_toggle_preserves_ordinary_fingerprints_scores_and_optimizer_inputs(department, amendment):
    from apps.api.services import planning
    from packages.domain.score import schedule_score
    from packages.domain import cp_sat
    from packages.domain.solver_control import SolverControl
    from packages.contracts.schemas import GenerationOptions
    h = department
    base = h.publish(h.shift(h.workspace('Baseline'))['workspace'])['publication_id'] if amendment else None
    draft = h.shift(h.workspace('Ordinary draft', base=base))['workspace']
    def evidence():
        with transaction(h.app.state.engine) as db:
            workspace = db.get(m.Workspace, draft['id'])
            snapshot = snapshots.domain_snapshot(db, workspace)
            # Compare the actual compiled optimizer input; snapshots also retain changing audit metadata.
            options = GenerationOptions.model_validate(snapshot.get('generation_options') or {})
            prepared = cp_sat._prepare(copy.deepcopy(snapshot), 1, 1, SolverControl(), options, False, False)
            assert 'model' in prepared, prepared.get('status')
            checked = planning.revalidate(db, workspace, store=False)
            if amendment:
                assert checked['scope'] == 'published_changes' and 'score' not in checked
            else:
                assert not checked.get('scope')
                defaults = db.get(m.Department, workspace.department_id).generation_defaults
                assert checked['score'] == schedule_score(snapshot, checked, defaults)
            return (snapshots.fingerprint(db, workspace), str(prepared['model'].proto),
                    checked.get('score'), checked['findings'])
    job = h.command('coordinator', 'job.create', {'workspace_id': draft['id'], 'kind': 'generation'},
                    revision=draft['revision'])['job']
    before = evidence()
    setting(h, True)
    assert evidence() == before
    setting(h, False, role='admin')
    assert evidence() == before
    with transaction(h.app.state.engine, write=True) as db:
        assert db.get(m.Job, job['id']).input_snapshot.get('score_baseline') is None
        assert claim_job(db)['id'] == job['id']
