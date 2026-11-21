"""Diagnostic follow-ups reuse exclusions, while compiling their own constraints."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from ortools import __version__ as solver_version
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.worker import generation_preparation
from apps.worker.generation_preparation import MAX_REUSE_EVIDENCE_ROWS, candidate_evidence_for_followup
from packages.contracts.schemas import GenerationOptions
from packages.domain import cp_sat
from packages.domain.solver_control import SolverControl
from packages.domain.solver_rules import COMPILER_VERSION


@pytest.fixture
def followup():
    engine = create_engine('sqlite://')
    m.ReadProjectionWarmCursor.__table__.create(engine)
    m.Job.__table__.create(engine)
    now = datetime(2026, 10, 3, 6, tzinfo=timezone.utc)
    snapshot = {'fingerprint': 'frozen-inputs', 'workspace': {'id': 'workspace'},
                'generation_engine': 'cp_sat', 'compiler_version': COMPILER_VERSION,
                'generation_options': GenerationOptions().model_dump(mode='json'), 'diagnostic_proposal': False}
    evidence = [{'shift_id': 'shift', 'person_id': 'person', 'eligible_and_available': False,
                 'exclusions': [{'code': 'UNAVAILABLE'}]}]
    result = {'status': 'requirements_conflict', 'algorithm_version': COMPILER_VERSION,
              'solver_version': solver_version, 'input_fingerprint': snapshot['fingerprint'],
              'candidate_evidence': evidence}
    with Session(engine) as db, db.begin():
        for identifier, status, diagnostic, created, outcome in (
                ('strict', 'succeeded', False, now, result),
                ('diagnostic', 'running', True, now + timedelta(seconds=3), None)):
            db.add(m.Job(id=identifier, department_id='department', workspace_id='workspace',
                requested_by='coordinator', kind='generation', status=status, attempts=1,
                created_at=created, completed_at=created if outcome else None,
                input_fingerprint=snapshot['fingerprint'],
                input_snapshot={**snapshot, 'diagnostic_proposal': diagnostic}, result=outcome,
                read_metadata={}, result_summary={}))
    claim = {'id': 'diagnostic', 'kind': 'generation', 'attempt': 1,
             'input': {**deepcopy(snapshot), 'diagnostic_proposal': True}, 'resume_state': None}
    try:
        yield engine, claim, evidence
    finally:
        engine.dispose()


def test_followup_reads_only_one_predecessor_without_rewriting_private_state(followup):
    engine, claim, evidence = followup
    statements = []
    event.listen(engine, 'before_cursor_execute', lambda connection, cursor, sql, *args: statements.append(sql))
    assert candidate_evidence_for_followup(engine, claim) == evidence
    assert len(statements) == 1 and 'LIMIT' in statements[0]
    assert 'resume_state' not in statements[0] and 'resume_checkpoint' not in statements[0]


@pytest.mark.parametrize('change', ['fingerprint', 'session_scope', 'options', 'defaults', 'input_fact',
    'compiler', 'solver', 'diagnostic_source', 'source_status', 'outcome', 'result_fingerprint',
    'missing_evidence', 'intervening', 'current_cancelled', 'current_attempt', 'engine', 'invalid_options'])
def test_followup_rejects_stale_or_ineligible_evidence(followup, change):
    engine, claim, evidence = followup
    with Session(engine) as db, db.begin():
        source = db.get(m.Job, 'strict')
        if change == 'fingerprint':
            claim['input']['fingerprint'] = 'different'
        elif change == 'session_scope':
            claim['input']['generation_session_ids'] = ['different-session']
        elif change == 'options':
            claim['input']['generation_options']['mode'] = 'rebalance'
        elif change == 'defaults':
            claim['input']['generation_defaults'] = {'mode': 'rebalance'}
        elif change == 'input_fact':
            claim['input']['availability'] = [{'person_id': 'person', 'start': 'changed'}]
        elif change in {'compiler', 'solver', 'outcome', 'result_fingerprint', 'missing_evidence'}:
            key, value = {'compiler': ('algorithm_version', 'old'), 'solver': ('solver_version', 'old'),
                          'outcome': ('status', 'needs_more_time'),
                          'result_fingerprint': ('input_fingerprint', 'different'),
                          'missing_evidence': ('candidate_evidence', None)}[change]
            source.result = ({name: part for name, part in source.result.items() if name != key}
                             if change == 'missing_evidence' else {**source.result, key: value})
        elif change == 'diagnostic_source':
            source.input_snapshot = {**source.input_snapshot, 'diagnostic_proposal': True}
        elif change == 'source_status':
            source.status = 'cancelled'
        elif change == 'intervening':
            db.add(m.Job(id='intervening', department_id='department', workspace_id='workspace',
                requested_by='coordinator', kind='generation', status='failed',
                created_at=source.created_at + timedelta(seconds=1), input_fingerprint='different',
                input_snapshot={}, read_metadata={}, result_summary={}))
        elif change == 'current_cancelled':
            db.get(m.Job, claim['id']).status = 'cancelled'
        elif change == 'current_attempt':
            claim['attempt'] += 1
        elif change == 'engine':
            claim['input']['generation_engine'] = 'legacy'
        else:
            claim['input']['generation_options']['search_minutes'] = -1
    assert candidate_evidence_for_followup(engine, claim) is None


def test_followup_runtime_budget_change_does_not_invalidate_candidate_facts(followup):
    engine, claim, evidence = followup
    claim['input']['generation_options']['search_minutes'] += 1
    assert candidate_evidence_for_followup(engine, claim) == evidence


@pytest.mark.parametrize('row_count', [32_959, MAX_REUSE_EVIDENCE_ROWS, MAX_REUSE_EVIDENCE_ROWS + 1])
def test_followup_bounds_predecessor_evidence_before_semantic_comparison(followup, monkeypatch, row_count):
    engine, claim, evidence = followup
    evidence = evidence * row_count
    with Session(engine) as db, db.begin():
        source = db.get(m.Job, 'strict')
        source.result = {**source.result, 'candidate_evidence': evidence}
    if row_count > MAX_REUSE_EVIDENCE_ROWS:
        monkeypatch.setattr(generation_preparation, 'resume_key',
                            lambda *args: pytest.fail('Oversized evidence must skip semantic comparison'))
    reused = candidate_evidence_for_followup(engine, claim)
    assert reused == (evidence if row_count <= MAX_REUSE_EVIDENCE_ROWS else None)


@pytest.mark.parametrize('seed_kind', ['complete', 'sparse', 'different_interval'])
def test_diagnostic_evidence_reuse_compiles_fresh_constraints_and_fills_missing_rows(h, monkeypatch, seed_kind):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(workspace)
    for person in data['people'].values():
        person['active'] = False
    options = GenerationOptions.model_validate(data.get('generation_options') or {})
    reports, models = [], []
    original = cp_sat.candidate_evidence

    def observed(*args):
        report = original(*args)
        reports.append(report)
        return report

    monkeypatch.setattr(cp_sat, 'candidate_evidence', observed)

    def prepare(diagnostic, seed=None):
        return cp_sat._prepare(data, 1, 1, SolverControl(), options, False, diagnostic,
            _on_objectives=lambda model, tiers: models.append(str(model.proto)), _candidate_evidence=seed)

    assert prepare(False)['status'] == 'model_ready'
    seed = deepcopy(reports)
    reports.clear()
    assert prepare(True)['status'] == 'model_ready'
    fresh_reports = deepcopy(reports)
    assert models[0] != models[1], 'Diagnostic constraints must differ from the strict model'
    if seed_kind == 'sparse':
        seed.pop()
    elif seed_kind == 'different_interval':
        seed[0]['end'] = '2026-10-12T00:00:00+00:00'
    reports.clear()
    reused = prepare(True, seed)
    expected = len(fresh_reports) - (seed_kind != 'complete')
    assert reused['status'] == 'model_ready' and reused['candidate_evidence_reused'] == expected
    assert len(reports) == len(fresh_reports) - expected
    assert models[2] == models[1], 'Reusing exclusions must preserve the fresh diagnostic model'
