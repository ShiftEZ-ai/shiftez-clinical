"""Cycle 5: scoped cover, candidate pools and final proposal status agree across projections."""
from tests.historical_staffing import retained_staffing
import copy
import time

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import job_outcome, job_result_metadata, job_result_summary
from packages.contracts.schemas import GenerationOutcome
from packages.domain.coverage import coverage_places, coverage_rows
from packages.domain.generation_outcome import generation_outcome
from packages.domain.rota_summary import rota_summary
from packages.domain.validation import evaluate, finding_observations
from tests.legacy_lead import historical_type


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_scoped_cover_counts_match_api_summary_home_fix_list_and_grid(h):
    typ = h.configure('assignment_types', {'name': 'Two cover places', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}]}})['record']['id']
    # A person scope is evaluated by the same minimum-cover path as a rank/skill scope.
    h.policy({'kind': 'minimum_cover', 'minimum': 2}, {'person_id': h.seed['people'][0]})
    w = h.workspace()
    for day in range(4, 9):
        w = h.shift(w, f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00',
                    typ=typ, people=h.seed['people'][:2])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    summary = h.get('coordinator', f"/api/workspaces/{w['id']}/summary")
    assert (summary['places_needed'], summary['places_filled'], summary['places_open']) == (10, 5, 5)
    assert summary['fix_list']['summary']['places_open'] == 5
    assert summary['fix_list']['summary']['must'] > 0
    grid = h.detail(w)['workspace']['validation']['coverage']
    assert len(grid) == 5
    for interval in grid:
        assert (interval['required'], interval['available_eligible']) == (2, 2)
        assert (interval['places_needed'], interval['places_filled'], interval['places_open']) == (2, 1, 1)
    listed = next(row for row in h.get('coordinator', '/api/workspaces')['workspaces'] if row['id'] == w['id'])
    assert listed['health']['open_places'] == 5
    home = h.get('coordinator', '/api/home')
    assert any(row['key'] == 'blocking' and row['count'] > 0 for row in home['attention'])
    assert not any(row['key'] == 'ready_to_submit' and row['count'] > 0 for row in home['attention'])


def test_assigned_cover_and_potential_pool_have_explicit_bases(h):
    w = h.shift(h.workspace())['workspace']
    data = h.snapshot(w)
    from tests.test_finding_causes import policy
    data['policies'] = []
    policy(data, {'kind': 'minimum_cover', 'minimum': 2})
    finding = next(f for f in finding_observations(evaluate(data)) if f['code'] == 'SCOPED_COVERAGE_GAP')
    assert finding['detail']['available'] == finding['cause']['actual'] == 1
    assert finding['cause']['required'] == 2
    assert finding['cause']['eligible_people'] > finding['cause']['actual']
    assert finding['cause']['eligible_people_basis'] == 'potential_cover_known_records'


@pytest.mark.parametrize('code', ['SCOPED_COVERAGE_GAP', 'CAPABILITY_GAP', 'DISTINCT_PERSON_GAP',
                                  'SUPERVISION_GAP'])
def test_retained_skill_and_scoped_shortages_do_not_report_fully_filled(code):
    check = {'coverage': [{'shift_id': 's', 'required': 2, 'available_eligible': 3, 'status': 'gap'}],
             'findings': [{'code': code, 'severity': 'error', 'shift_id': 's',
                           'detail': {'required': 2, 'available': 1}}]}
    original = copy.deepcopy(check)
    assert coverage_rows(check)[0]['places_filled'] == 1
    assert rota_summary(check)['places_open'] == 1
    assert check == original


def test_lead_gap_is_role_only_but_still_blocks_readiness_and_seat_shortage_counts(h):
    typ = historical_type(h, {'name': 'Lead-only place regression', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}],
                       'lead': {'minimum': 1, 'maximum': 1,
                                'capability_ids': [h.seed['supervisor_capability']], 'category_ids': []}}})['id']
    a, b = h.seed['people'][:2]

    full = retained_staffing(h, h.workspace('Full seats, missing lead'), typ=typ, people=[a, b])['workspace']
    full = h.lifecycle(full, 'validate')['workspace']
    check = full['validation']
    assert any(f['code'] == 'LEAD_GAP' and f['severity'] == 'error' for f in check['findings'])
    assert check['valid'] is False
    assert [(r['places_needed'], r['places_filled'], r['places_open']) for r in check['coverage']] == [(2, 2, 0)]
    summary = h.get('coordinator', f"/api/workspaces/{full['id']}/summary")
    assert (summary['places_needed'], summary['places_filled'], summary['places_open']) == (2, 2, 0)
    assert summary['sessions'][0]['has_gap'] and summary['sessions'][0]['other_gaps']
    assert summary['fix_list']['summary']['must'] > 0
    assert summary['fix_list']['summary']['submit_blocked']
    listed = next(row for row in h.get('coordinator', '/api/workspaces')['workspaces'] if row['id'] == full['id'])
    assert listed['health']['valid'] is False and listed['health']['open_places'] == 0
    home = h.get('coordinator', '/api/home')
    assert not any(row['key'] == 'ready_to_submit' and row['count'] > 0 for row in home['attention'])

    short = retained_staffing(h, h.workspace('Seat shortage and missing lead'), typ=typ, people=[a])['workspace']
    short_check = evaluate(h.snapshot(short))
    assert {'COVERAGE_GAP', 'LEAD_GAP'} <= {f['code'] for f in short_check['findings'] if f['severity'] == 'error'}
    assert [(r['places_needed'], r['places_filled'], r['places_open']) for r in short_check['coverage']] == [(2, 1, 1)]
    assert rota_summary(short_check)['places_open'] == 1


def test_legacy_lead_requirement_without_seats_is_a_role_gap_not_an_open_place(h):
    definition = {'requirements': [{'minimum': 0, 'maximum': 0}],
                   'lead': {'minimum': 1, 'maximum': 1, 'capability_ids': [], 'category_ids': []}}
    h.configure('assignment_types', {'name': 'Contradictory lead authoring', 'starts_on': '2020-01-01',
                                     'definition': definition}, status=422)
    typ = historical_type(h, {'name': 'Legacy zero-seat lead duty', 'starts_on': '2020-01-01',
                              'definition': definition})['id']
    w = h.shift(h.workspace('Legacy zero-seat lead'), typ=typ, people=[])['workspace']
    check = evaluate(h.snapshot(w))
    assert any(f['code'] == 'LEAD_GAP' and f['severity'] == 'error' for f in check['findings'])
    assert check['valid'] is False
    assert [(row['places_needed'], row['places_filled'], row['places_open']) for row in check['coverage']] == [(0, 0, 0)]
    summary = h.get('coordinator', f"/api/workspaces/{w['id']}/summary")
    assert (summary['places_needed'], summary['places_filled'], summary['places_open']) == (0, 0, 0)
    assert summary['sessions'][0]['has_gap']


def test_overlapping_requirements_intervals_advisory_and_closed_rows():
    row = {'shift_id': 's', 'required': 2, 'available_eligible': 2, 'status': 'gap'}
    assert coverage_places(row, [{'required': 2, 'available': 1}] * 20) == {
        'places_needed': 2, 'places_filled': 1, 'places_open': 1}
    check = {'coverage': [{**row, 'start': '2026-10-04T05:00:00Z', 'end': '2026-10-04T06:00:00Z'},
                          {**row, 'start': '2026-10-04T06:00:00Z', 'end': '2026-10-04T07:00:00Z'}],
             'findings': [{'code': 'SCOPED_COVERAGE_GAP', 'severity': 'error', 'shift_id': 's',
                           'start': '2026-10-04T05:00:00Z', 'end': '2026-10-04T06:00:00Z',
                           'detail': {'required': 3, 'available': 1}},
                          {'code': 'SCOPED_COVERAGE_GAP', 'severity': 'warning', 'shift_id': 's',
                           'detail': {'required': 20, 'available': 0}}]}
    assert [r['places_open'] for r in coverage_rows(check)] == [2, 0]
    assert rota_summary(check)['places_open'] == 2
    assert coverage_places({**row, 'status': 'closed'}, [{'required': 3, 'available': 0}])['places_needed'] == 0


def test_scoped_minimum_is_retained_even_when_met(h):
    from tests.test_finding_causes import policy
    data = h.snapshot(h.shift(h.workspace(), people=h.seed['people'][:2])['workspace'])
    data['policies'] = []
    policy(data, {'kind': 'minimum_cover', 'minimum': 2})
    check = evaluate(data)
    assert check['coverage'][0]['required'] == 1  # original headcount evidence remains intact
    assert check['coverage'][0]['places_needed'] == check['coverage'][0]['places_filled'] == 2
    assert rota_summary(check)['places_needed'] == 2


@pytest.mark.parametrize('valid,optimal,strict,expected,candidate', [
    (True, False, True, 'INFEASIBLE', 'with_gaps'), (True, True, False, 'UNKNOWN', 'with_gaps'),
    (False, False, True, 'INFEASIBLE', 'with_gaps'), (False, False, False, 'UNKNOWN', 'with_gaps'),
    (None, False, False, 'UNKNOWN', 'none')])
def test_final_status_is_not_a_diagnostic_or_objective_status(valid, optimal, strict, expected, candidate):
    result = {'status': 'candidate_with_gaps', 'validation': {'valid': valid}, 'optimality_proven': optimal,
              'weighted_status': 'OPTIMAL', 'diagnostic_proposal': True,
              **({'conflict': {'strict_status': 'INFEASIBLE'}} if strict else {})}
    outcome = generation_outcome(result)
    GenerationOutcome.model_validate(outcome)
    assert outcome['status'] == expected and outcome['candidate'] == candidate
    assert job_result_summary(result)['generation_outcome'] == outcome
    metadata = job_result_metadata(result)
    assert metadata['generation_outcome'] == outcome
    assert job_result_metadata(metadata) == metadata
    assert job_outcome('succeeded', result) == 'partial'


@pytest.mark.parametrize(('status', 'expected'), [('cancelled', 'cancelled'),
                                                  ('requirements_conflict', 'infeasible')])
def test_diagnostic_terminal_job_outcomes_keep_their_status(status, expected):
    assert job_outcome('cancelled' if status == 'cancelled' else 'succeeded',
                       {'status': status, 'diagnostic_proposal': True}) == expected


def test_diagnostic_proposal_that_passes_validation_remains_incomplete(h):
    from packages.domain.cp_sat import generate
    from apps.worker.main import recover_checkpoint_candidate, validated_proposal
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    result = generate(data, 3, diagnostic_proposal=True, deterministic=True)
    assert result['validation']['valid'], result
    assert result['diagnostic_proposal'] is True
    assert result['status'] == 'candidate_with_gaps'
    assert result['generation_outcome']['status'] == 'UNKNOWN'
    assert result['generation_outcome']['candidate'] == 'with_gaps'
    assert not result['optimality_proven']
    assert 'Incomplete diagnostic proposal' in result.get('message', '')
    stale = {**result, 'status': 'candidate_feasible', 'diagnostic_proposal': True,
             'generation_outcome': {'status': 'FEASIBLE', 'candidate': 'valid',
                                    'strict_status': None, 'optimality_proven': False}}
    assert not validated_proposal(stale, data['fingerprint'])
    assert recover_checkpoint_candidate(data, stale)['status'] == 'candidate_with_gaps'
    assert job_outcome('succeeded', stale) == 'partial'
    assert job_result_metadata(stale)['generation_outcome']['status'] == 'UNKNOWN'


def test_checkpointed_diagnostic_recovery_is_incomplete(h):
    from packages.domain.cp_sat import generate
    from apps.worker.main import recover_checkpoint_candidate
    from packages.domain.generation_outcome import DIAGNOSTIC_PROPOSAL_MESSAGE
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    result = generate(data, 3, diagnostic_proposal=True, deterministic=True)
    checkpoint = {**result, 'status': 'candidate_feasible', 'checkpointed': True,
                  'diagnostic_proposal': True, 'optimality_proven': True}
    checkpoint.pop('validation', None)
    recovered = recover_checkpoint_candidate(data, checkpoint)
    assert recovered['status'] == 'candidate_with_gaps'
    assert recovered['optimality_proven'] is False
    assert recovered['message'] == DIAGNOSTIC_PROPOSAL_MESSAGE


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_job_api_preserves_final_outcome_separately_from_strict_proof(h):
    w = h.workspace()
    raw = {'status': 'candidate_with_gaps', 'validation': {'valid': True, 'error_count': 0},
           'conflict': {'strict_status': 'INFEASIBLE'}, 'diagnostic_proposal': True,
           'additions': [], 'removals': [], 'optimality_proven': False}
    with transaction(h.app.state.engine, write=True) as db:
        job = m.Job(department_id=h.seed['department_id'], workspace_id=w['id'],
                    requested_by=h.seed['identities']['coordinator'], kind='generation', status='succeeded',
                    input_fingerprint='test', input_snapshot={}, result=raw)
        db.add(job)
        db.flush()
        job_id = job.id
    for path in [f'/api/jobs/{job_id}', f'/api/jobs/{job_id}?full=true']:
        result = h.get('coordinator', path)['job']['result']
        assert result['generation_outcome']['status'] == 'INFEASIBLE'
        assert result['conflict']['strict_status'] == 'INFEASIBLE'
    history = h.get('coordinator', f"/api/jobs?workspace_id={w['id']}")['jobs'][0]
    assert history['result']['generation_outcome']['status'] == 'INFEASIBLE'
    with transaction(h.app.state.engine) as db:
        from apps.api.services.job_reads import status_metadata
        assert status_metadata(db, job_id)['result']['generation_outcome']['status'] == 'INFEASIBLE'
        assert db.get(m.Job, job_id).result == raw


def test_department_scale_coverage_projection_is_bounded():
    from tests.test_scheduler_performance import _budget
    check = {'coverage': [{'shift_id': str(i), 'required': 2, 'available_eligible': 2, 'status': 'gap'}
                          for i in range(500)],
             'findings': [{'shift_id': str(i), 'code': 'SCOPED_COVERAGE_GAP', 'severity': 'error',
                           'detail': {'required': 2, 'available': 1}} for i in range(500)]}
    start = time.process_time()
    result = rota_summary(check, compact=True)
    elapsed = time.process_time() - start
    assert (result['places_needed'], result['places_filled'], result['places_open']) == (1000, 500, 500)
    print(f'500-session coverage summary CPU: {elapsed:.3f}s')
    return elapsed


@pytest.mark.cpu_budget
def test_department_scale_coverage_projection_cpu_budget():
    elapsed = test_department_scale_coverage_projection_is_bounded()
    from tests.test_scheduler_performance import _budget
    assert elapsed < _budget(1), elapsed


def test_metadata_keeps_optimality_and_strict_proof_without_full_evidence():
    source = {'status': 'candidate_feasible', 'validation': {'valid': True, 'error_count': 0},
              'optimality_proven': True, 'conflict': {'strict_status': 'INFEASIBLE'}}
    metadata = job_result_metadata(source)
    assert job_result_summary(metadata)['generation_outcome'] == generation_outcome(source)
    # A time-limited legacy search can retain a fully validated incumbent without an optimality proof.
    source.update(status='unknown_time_limit', optimality_proven=False)
    assert generation_outcome(source)['status'] == 'FEASIBLE'


def test_generation_receipt_survives_result_summary_metadata_and_contract():
    from apps.api.serialization import job_result_metadata, job_result_summary
    from packages.contracts.schemas import JobResult

    raw = {'status': 'candidate_feasible', 'solver_status': 'FEASIBLE', 'score': 87.5,
           'stop_reason': 'time_limit',
           'search_stop': {'reason': 'time_limit', 'search_seconds': 300, 'detail': 'Search time used up.'}}
    detail = job_result_summary(raw)
    history = job_result_metadata(raw)
    typed = JobResult.model_validate(detail)

    assert typed.solver_status == history['solver_status'] == 'FEASIBLE'
    assert typed.stop_reason == history['stop_reason'] == 'time_limit'
    assert typed.score == history['score'] == 87.5
    assert typed.search_stop.reason == history['search_stop']['reason'] == 'time_limit'


def test_pooled_gap_is_counted_once_but_each_peer_can_repair_it():
    from packages.domain.coverage import session_open_places
    check = {'coverage': [{'shift_id': sid, 'required': 0, 'available_eligible': 1, 'status': 'covered'}
                          for sid in ['anchor', 'peer']],
             'findings': [{'code': 'SCOPED_COVERAGE_GAP', 'severity': 'error', 'shift_id': 'anchor',
                           'detail': {'required': 3, 'available': 2, 'affected_shift_ids': ['anchor', 'peer']}}]}
    assert rota_summary(check)['places_open'] == 1
    assert session_open_places(check, 'anchor') == session_open_places(check, 'peer') == 1
