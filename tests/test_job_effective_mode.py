"""A same-job diagnostic continuation is visible without changing the frozen request."""
from copy import deepcopy
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import job_result_metadata
from apps.api.services.job_reads import metadata


@pytest.mark.parametrize('snapshot', [{}, {'run_context': None}])
def test_missing_run_context_is_unknown_without_loading_inputs_or_mutating_projection(snapshot):
    retained = {'input_snapshot': snapshot, 'result': None}
    original = deepcopy(retained)
    job = SimpleNamespace(read_metadata=retained)
    assert metadata(job) == {'input_snapshot': {'run_context': None}, 'result': None}
    assert retained == original


@pytest.mark.parametrize('source', ['result', 'progress', 'input'])
@pytest.mark.parametrize('job_count', [1, 2, 3])
def test_jobs_project_effective_diagnostic_mode_without_loading_frozen_inputs(h, source, job_count):
    workspace = h.workspace()
    other_ids = {h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': kind},
                           revision=workspace['revision'])['job']['id']
                 for kind in ('validation', 'feasibility')[:job_count - 1]}
    snapshot = {'generation_engine': 'cp_sat', 'diagnostic_proposal': source == 'input',
                'focus_ids': ['draft-session'], 'retained_inputs': ['unchanged'] * 1000}
    result = None if source == 'progress' else {'status': 'requirements_conflict',
        **({'diagnostic_proposal': True} if source == 'result' else {}),
        'candidate_evidence': [{'person_id': 'synthetic', 'exclusions': []}] * 1000}
    pairs = [{'shift_id': shift, 'person_id': 'synthetic'} for shift in ('draft-session', 'foreign-session')]
    progress = {'stage': 'improving', 'diagnostic_proposal': source == 'progress',
                'events': [{'adds': pairs, 'removes': []}], 'solution': {'pairs': pairs}}
    with transaction(h.app.state.engine, write=True) as db:
        job = m.Job(department_id=h.seed['department_id'], workspace_id=workspace['id'],
            requested_by=h.seed['identities']['coordinator'], kind='generation',
            status='running' if source == 'progress' else 'succeeded', input_fingerprint='frozen',
            input_snapshot=snapshot, result=result,
            progress=progress)
        db.add(job)
        db.flush()
        job_id = job.id
    h.client('coordinator')
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if 'FROM jobs' in statement:
            statements.append((statement, _parameters))

    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        history = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs']
        detail = h.get('coordinator', '/api/jobs/' + job_id)['job']
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    assert {job['id'] for job in history} == other_ids | {job_id}
    assert len(history) == job_count
    assert history[0]['id'] == job_id
    assert history[0]['diagnostic_proposal'] is detail['diagnostic_proposal'] is True
    if source == 'result':
        assert history[0]['result']['diagnostic_proposal'] is detail['result']['diagnostic_proposal'] is True
        assert history[0]['result']['status'] == detail['result']['status'] == 'requirements_conflict'
    assert 'events' not in history[0]['progress'] and 'solution' not in history[0]['progress']
    if source == 'progress':
        assert detail['progress']['events'] == [{'adds': pairs[:1], 'removes': []}]
        assert detail['progress']['solution'] == {'pairs': pairs[:1]}
    else:
        assert 'events' not in detail['progress'] and 'solution' not in detail['progress']
    focus_reads = [(sql, parameters) for sql, parameters in statements if 'jobs.input_snapshot' in sql]
    assert len(focus_reads) == int(source == 'progress')
    if focus_reads:
        sql, parameters = focus_reads[0]
        assert 'JSON_EXTRACT(jobs.input_snapshot,' in sql
        assert sql.count('jobs.input_snapshot') == 1
        # Only the focus ids of this job; the private-draft access filter (D18, workspace_access.py) follows them.
        assert parameters[:2] == ('$."focus_ids"', job_id)
    # The private-draft access clause adds one bounded workspace lookup.
    assert len(statements) == 3 + len(focus_reads)
    assert not any(re.search(r'jobs\.(?:result|resume_checkpoint|resume_state)\b', sql)
                   for sql, _ in statements)
    full = h.get('coordinator', '/api/jobs/' + job_id + '?full=true')['job']
    assert full['diagnostic_proposal'] is True
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Job, job_id)
        assert stored.input_snapshot == snapshot and stored.result == result and stored.progress == progress


@pytest.mark.parametrize('mode', [False, True])
def test_job_result_metadata_preserves_an_explicit_diagnostic_mode(mode):
    assert job_result_metadata({'status': 'requirements_conflict', 'diagnostic_proposal': mode}) == {
        'status': 'requirements_conflict', 'diagnostic_proposal': mode,
        'generation_outcome': {'status': 'INFEASIBLE', 'candidate': 'none',
                               'strict_status': 'INFEASIBLE', 'optimality_proven': False}}


def test_job_result_metadata_keeps_the_applied_revision_for_undo_in_recent_runs():
    """An applied run under a newer rota check (maintenance queues one after Apply) still offers Undo from the job list."""
    assert job_result_metadata({'status': 'candidate_feasible', 'additions': [{'shift_id': 's'}], 'applied_content_revision': 4}) == {
        'status': 'candidate_feasible', 'applied_content_revision': 4, 'change_count': 1,
        'generation_outcome': {'status': 'UNKNOWN', 'candidate': 'none',
                               'strict_status': None, 'optimality_proven': False}}


@pytest.mark.parametrize('proposal,count', [({'additions': [], 'removals': []}, 0),
    ({'additions': [{}]}, 1), ({'removals': [{}, {}]}, 2),
    ({'additions': [{}], 'removals': [{}], 'change_count': 999}, 2)])
def test_job_history_change_count_uses_proposal_and_survives_metadata_projection(proposal, count):
    source = {'status': 'candidate_feasible', **proposal}
    metadata = job_result_metadata(source)
    assert metadata == {'status': 'candidate_feasible', 'change_count': count,
        'generation_outcome': {'status': 'UNKNOWN', 'candidate': 'none',
                               'strict_status': None, 'optimality_proven': False}}
    assert job_result_metadata(metadata) == metadata
