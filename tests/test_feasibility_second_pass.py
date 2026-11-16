"""Only a timed-out phase gets one longer, frozen feasibility attempt."""
from copy import deepcopy
from dataclasses import replace

import pytest

from apps.api.main import create_app
from apps.api.settings import Settings
from packages.domain import feasibility


def snapshot(**fields):
    data = {'workspace': {'status': 'draft'}, 'shifts': [], 'focus_ids': [], 'people': {}, 'policies': []}
    return {'baseline': data, 'proposed': deepcopy(data), 'bindings': [], 'fingerprint': 'same-inputs',
            'check_seconds': 10, 'second_pass_seconds': 70, **fields}


@pytest.mark.parametrize('second_status', ['candidate_feasible', 'unknown_time_limit', 'requirements_conflict', 'cancelled'])
def test_unknown_gets_exactly_one_longer_attempt_and_keeps_literal_status(monkeypatch, second_status):
    calls = []
    def generate(data, seconds, **kwargs):
        calls.append((seconds, kwargs['workers'], kwargs['feasibility_only']))
        status = 'unknown_time_limit' if seconds == 10 else second_status
        return {'status': status, 'solver_status': {'candidate_feasible': 'FEASIBLE',
            'requirements_conflict': 'INFEASIBLE'}.get(status, 'UNKNOWN'),
            'candidate_evidence': [], 'diagnostics': [{'code': 'PROVEN_CONFLICT'}] if status == 'requirements_conflict' else [],
            'additions': ['not-applicable'], 'removals': ['not-applicable'], 'optimality_proven': False}
    monkeypatch.setattr(feasibility, 'generate', generate)
    monkeypatch.setattr(feasibility, 'evaluate', lambda data: {'findings': []})
    result = feasibility.check(snapshot())
    assert calls == [(10, 1, True), (70, 1, True)] * (1 if second_status == 'cancelled' else 2)
    assert result['status'] == {'candidate_feasible': 'feasible', 'requirements_conflict': 'requirements_conflict',
                               'unknown_time_limit': 'inconclusive', 'cancelled': 'cancelled'}[second_status]
    if second_status != 'cancelled':
        assert result['comparison_complete'] is (second_status != 'unknown_time_limit')
        assert result['full_feasibility_established'] is (second_status == 'candidate_feasible')
    for check in result['checks'].values():
        assert [a['limit_seconds'] for a in check['search_attempts']] == [10, 70]
        assert check['search_attempts'][0]['solver_status'] == 'UNKNOWN'
        assert check['search_attempts'][-1]['status'] == second_status
        assert not check['optimality_proven']
        assert 'additions' not in check and 'removals' not in check


@pytest.mark.parametrize('status', ['candidate_feasible', 'requirements_conflict', 'invalid_inputs', 'model_invalid', 'cancelled'])
def test_other_outcomes_never_get_a_second_attempt(monkeypatch, status):
    calls = []
    def generate(data, seconds, **kwargs):
        calls.append(seconds)
        return {'status': status, 'candidate_evidence': [], 'diagnostics': []}
    monkeypatch.setattr(feasibility, 'generate', generate)
    monkeypatch.setattr(feasibility, 'evaluate', lambda data: {'findings': []})
    feasibility.check(snapshot())
    assert calls == [10] * (1 if status == 'cancelled' else 2)


def test_legacy_frozen_job_keeps_its_original_single_pass_budget(monkeypatch):
    calls = []
    monkeypatch.setattr(feasibility, 'generate', lambda data, seconds, **kwargs:
        calls.append(seconds) or {'status': 'unknown_time_limit'})
    legacy = snapshot()
    del legacy['second_pass_seconds']
    result = feasibility.check(legacy)
    assert calls == [10, 10] and not result['comparison_complete']


@pytest.mark.parametrize('value', [0, 601])
def test_second_pass_setting_rejects_unbounded_or_empty_limits(value):
    with pytest.raises(RuntimeError, match='ROTA_FEASIBILITY_SECOND_PASS_SECONDS'):
        replace(Settings(environment='test'), feasibility_second_pass_seconds=value).check(require_authentication=False)


def test_second_pass_setting_reads_environment(monkeypatch):
    monkeypatch.setenv('ROTA_FEASIBILITY_SECOND_PASS_SECONDS', '90')
    assert Settings.from_env().feasibility_second_pass_seconds == 90


@pytest.mark.parametrize('first,expected', [(10, 90), (60, 120)])
def test_new_job_freezes_setting_with_a_strictly_longer_budget(h, first, expected):
    from apps.api import models as m
    from apps.api.db import transaction
    h.settings = replace(h.settings, feasibility_second_pass_seconds=90)
    h.app = create_app(h.settings, engine=h.app.state.engine)
    workspace = h.workspace()
    job = h.command('coordinator', 'job.create', {'kind': 'feasibility', 'workspace_id': workspace['id'],
        'check_seconds': first}, revision=workspace['revision'])['job']
    with transaction(h.app.state.engine) as db:
        frozen = db.get(m.Job, job['id']).input_snapshot
        assert frozen['second_pass_seconds'] == expected
        assert frozen['check_seconds'] == first
