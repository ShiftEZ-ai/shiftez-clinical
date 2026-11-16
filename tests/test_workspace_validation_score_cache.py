"""Retained edit checks keep their evidence while score reads reuse semantic inputs."""
from collections import Counter
from copy import deepcopy
import json

import pytest
from sqlalchemy import delete, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import rota_summary as service
from packages.domain.rota_summary import normalized_check
from tests.clock_support import freeze_clock
from tests.test_backend_scale import measure, post
from tests.test_draft_edit_scale import department_draft
from tests.test_performance_budget import python_budget


def retained_check(h, workspace, *, remove_assignment=False):
    """Model the scoreless retained evidence while the ordinary validation worker is pending."""
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, workspace['id'])
        if remove_assignment:
            assignment = db.scalar(select(m.Assignment).join(m.Shift).where(m.Shift.workspace_id == row.id))
            db.delete(assignment)
            row.revision += 1
            row.content_revision += 1
        check = deepcopy(row.validation)
        check.pop('score', None)
        row.validation = {**check, 'scope': 'command_changes'}


def read(h, workspace):
    with transaction(h.app.state.engine) as db:
        return service.workspace_validation(db, db.get(m.Workspace, workspace['id']))


def projection(h, workspace):
    with transaction(h.app.state.engine) as db:
        rows = list(db.scalars(select(m.WorkspaceReadProjection).where(
            m.WorkspaceReadProjection.workspace_id == workspace['id'],
            m.WorkspaceReadProjection.kind == 'workspace_score')))
        assert len(rows) == 1  # New stamps replace the same canonical row, rather than accumulating versions/readers.
        return rows[0].stamp, rows[0].audience, rows[0].value


@pytest.fixture
def baseline_draft(h):
    policy = h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, severity='advisory')
    workspace = h.shift(h.workspace(), people=[h.seed['people'][0]])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[h.seed['people'][0]])['workspace']
    retained_check(h, workspace)
    return h, workspace, policy


def test_content_currency_ignores_unrelated_and_frozen_edits_but_refresh_invalidates(baseline_draft, monkeypatch):
    h, workspace, policy = baseline_draft
    calls = []
    real = service.evaluate
    def evaluate(*args, **kwargs):
        assert kwargs.get('explain_findings', True)
        calls.append(1)
        return real(*args, **kwargs)
    monkeypatch.setattr(service, 'evaluate', evaluate)
    original = read(h, workspace)
    stamp, audience, _ = projection(h, workspace)
    assert calls == [1]
    calls.clear()
    assert read(h, workspace) == original  # Page check with no changed input.
    assert not calls
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        org.input_revision += 1
        org.availability_revision += 1
    assert read(h, workspace) == original
    assert projection(h, workspace)[:2] == (stamp, audience)
    assert not calls
    # An approved department advisory edit is outside the immutable draft baseline.
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, policy['id'])
        row.definition = {**row.definition, 'rule': {**row.definition['rule'], 'maximum': 9}}
        row.revision += 1
    assert read(h, workspace) == original
    assert projection(h, workspace)[:2] == (stamp, audience)
    assert not calls
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        department.generation_defaults = {'scoring': {'desired_cover_factor': 0.5}}
        department.revision += 1
    assert read(h, workspace) == original
    assert projection(h, workspace)[:2] == (stamp, audience)
    assert not calls
    preview = post(h, 'workspace.baseline.refresh.preview', {}, workspace['id'], workspace['revision'])
    refreshed = post(h, 'workspace.baseline.refresh.apply', {
        'expected_baseline_hash': preview['baseline']['snapshot_hash'],
        'decisions': {source: 'use_department' for source in preview['conflicts']},
        'reason': 'Adopt reviewed advisory and score settings'}, workspace['id'], workspace['revision'])['workspace']
    # Refresh clears validation; recreate the next normal retained edit check before the worker completes it.
    post(h, 'workspace.validate', {'reason': 'Check the refreshed baseline'}, refreshed['id'], refreshed['revision'])
    retained_check(h, refreshed)
    calls.clear()
    updated = read(h, refreshed)
    assert calls == [1]
    assert updated['score']['settings']['desired_cover_factor'] == 0.5
    assert updated['score'] != original['score']
    assert projection(h, workspace)[0] != stamp
    # A cold read after the live-default edit also uses the frozen defaults, rather than depending on a warm row.
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).generation_defaults = {'scoring': {'desired_cover_factor': 0.9}}
        db.execute(delete(m.WorkspaceReadProjection).where(
            m.WorkspaceReadProjection.workspace_id == workspace['id'], m.WorkspaceReadProjection.kind == 'workspace_score'))
    assert read(h, workspace)['score'] == updated['score']


def test_rota_advisory_adjustment_changes_score_and_replaces_projection(baseline_draft):
    h, workspace, policy = baseline_draft
    before = read(h, workspace)['score']
    stamp = projection(h, workspace)[0]
    changed = post(h, 'workspace.rule_adjust', {'policy_id': policy['id'], 'mode': 'off',
        'reason': 'Pause this advisory only on this rota'}, workspace['id'], workspace['revision'])['workspace']
    retained_check(h, changed)
    after = read(h, changed)['score']
    assert after['uncapped_score'] > before['uncapped_score']
    assert after['components'] != before['components']
    assert projection(h, workspace)[0] != stamp


def test_live_safety_rule_changes_effective_content_and_misses_cache(baseline_draft, monkeypatch):
    from packages.domain.input_manifest import manifest
    h, workspace, _ = baseline_draft
    before = read(h, workspace)
    stamp = projection(h, workspace)[0]
    snapshot = h.snapshot(workspace)
    authoritative = deepcopy(snapshot['input_manifest'])
    assert service._score_fingerprint(snapshot) == manifest({**snapshot, '_live_safety_staleness': None})['token']
    assert snapshot['input_manifest'] == authoritative
    assert manifest(snapshot) == authoritative  # Cache hashing never edits authoritative evidence.
    safety = h.policy({'kind': 'minimum_cover', 'minimum': 2})
    changed = h.snapshot(workspace)
    assert changed['_live_safety_staleness'] != snapshot['_live_safety_staleness']
    assert any(rule['id'] == safety['id'] for rule in changed['_live_safety_staleness']['policies'])
    assert any(rule['id'] == safety['id'] for rule in changed['policies'])
    assert changed['fingerprint'] != snapshot['fingerprint']
    calls = []
    real = service.evaluate
    monkeypatch.setattr(service, 'evaluate', lambda *args, **kwargs: calls.append(1) or real(*args, **kwargs))
    after = read(h, workspace)
    assert calls == [1]
    assert projection(h, workspace)[0] != stamp
    assert after['score'] != before['score']


@pytest.mark.parametrize('version', ['validator', 'score'])
def test_toolchain_change_recomputes_score(baseline_draft, monkeypatch, version):
    from packages.domain import score, validation
    h, workspace, _ = baseline_draft
    read(h, workspace)
    stamp = projection(h, workspace)[0]
    module, name = (validation, 'ENGINE_VERSION') if version == 'validator' else (score, 'VERSION')
    monkeypatch.setattr(module, name, getattr(module, name) + '-test-change')
    calls = []
    real = service.evaluate
    monkeypatch.setattr(service, 'evaluate', lambda *args, **kwargs: calls.append(1) or real(*args, **kwargs))
    read(h, workspace)
    assert calls == [1]
    assert projection(h, workspace)[0] != stamp


def test_projection_returns_independent_values_even_in_activation_memo(baseline_draft):
    h, workspace, _ = baseline_draft
    with transaction(h.app.state.engine) as db:
        db.info['_activation_read'] = True
        row = db.get(m.Workspace, workspace['id'])
        stored = deepcopy(row.validation)
        first = service.workspace_validation(db, row)
        expected = deepcopy(first['score'])
        first['score']['settings']['desired_cover_factor'] = -1
        first['score']['people']['not-a-person'] = {'private': True}
        assert service.workspace_validation(db, row)['score'] == expected
        assert row.validation == stored


def uncached_validation(db, workspace):
    """The pre-cache implementation, kept verbatim as the response-byte oracle."""
    validation = normalized_check(workspace.validation) or {}
    score = validation.get('score')
    if (validation.get('scope') != 'command_changes' or isinstance(score, dict)
            or not validation.get('evaluated_at')):
        return validation
    department = db.get(m.Department, workspace.department_id)
    data = service.domain_snapshot(db, workspace)
    complete = service.evaluate(data)
    return {**validation, 'score': service.schedule_score(data, complete, department.generation_defaults)}


@pytest.fixture(scope='module')
def workspace_score_measurements(scale_harness):
    h = scale_harness('h')
    with pytest.MonkeyPatch.context() as patch:
        freeze_clock(patch)
        workspace, people = department_draft(h, per_day=18, days=28)
        assert len(people) == 150
        retained_check(h, workspace, remove_assignment=True)
        with transaction(h.app.state.engine) as db:
            stored = deepcopy(db.get(m.Workspace, workspace['id']).validation)
            assert len(stored['coverage']) >= 500 and stored['findings']
        path = f'/api/workspaces/{workspace["id"]}?view=full'
        current, real_evaluate = service.workspace_validation, service.evaluate
        calls = Counter()
        def evaluate(*args, **kwargs):
            assert kwargs.get('explain_findings', True)
            calls['evaluate'] += 1
            return real_evaluate(*args, **kwargs)
        patch.setattr(service, 'evaluate', evaluate)
        observations, bodies = {}, {}
        for phase, implementation in (('before', uncached_validation), ('after', current)):
            patch.setattr(service, 'workspace_validation', implementation)
            for n in (1, 2):
                calls.clear()
                response, metrics = measure(h, f'workspace-score-{phase}-{n}', lambda: h.client('coordinator').get(path))
                assert response.status_code == 200, response.text
                metrics['evaluate_calls'] = calls['evaluate']
                observations[f'{phase}_{n}'] = metrics
                bodies[f'{phase}_{n}'] = response.content
        assert all(body == bodies['before_1'] for body in bodies.values())
        # The windowed findings endpoint consumes the same canonical score cache.
        path = f'/api/workspaces/{workspace["id"]}/parts?part=validation&start=2026-10-01&end=2026-10-29'
        patch.setattr(service, 'workspace_validation', uncached_validation)
        expected = h.client('coordinator').get(path)
        patch.setattr(service, 'workspace_validation', current)
        calls.clear()
        actual = h.client('coordinator').get(path)
        assert expected.status_code == actual.status_code == 200
        assert expected.content == actual.content
        assert not calls['evaluate']
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Workspace, workspace['id']).validation == stored
        # The canonical audience shares only the existing actor-independent score; reader disclosure stays downstream.
        assert h.get('admin', f'/api/workspaces/{workspace["id"]}')['workspace']['validation']['score'] == json.loads(bodies['after_2'])['workspace']['validation']['score']
        projection(h, workspace)
    return observations


def test_full_edited_reads_preserve_bytes_and_bound_work(workspace_score_measurements, request):
    values = workspace_score_measurements
    request.node.user_properties.append(('workspace_score_reads', json.dumps(values, sort_keys=True)))
    assert [values[f'before_{n}']['evaluate_calls'] for n in (1, 2)] == [1, 1]
    assert [values[f'after_{n}']['evaluate_calls'] for n in (1, 2)] == [1, 0]
    assert sum(values['after_2']['sql'].values()) <= 100
    assert sum(values['after_2']['sql'].values()) <= sum(values['before_2']['sql'].values()) + 5


@pytest.mark.wall_benchmark
def test_full_edited_second_read_wall_benchmark(workspace_score_measurements):
    assert workspace_score_measurements['after_2']['wall_seconds'] < python_budget(5), workspace_score_measurements
