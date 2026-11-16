"""The strict conflict policy is explicit, bounded and resumable."""

import copy

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import GenerationOptions, JobResult, SolverConflictDecision
from packages.domain.cp_sat import generate
from packages.domain.validation import finding_observations
from tests.test_solver_diagnostic_continuation import shortfall
from tests.test_template_scheduler_audit import use_cp_sat


def test_report_is_the_default_and_keeps_a_strict_checkpoint(h):
    data = shortfall(h, quota=True)
    saved = []
    result = generate(data, 3, deterministic=True,
                      checkpoint=lambda state: saved.append(copy.deepcopy(state)))

    assert GenerationOptions().on_conflict == 'report'
    assert result['status'] == 'requirements_conflict', result
    assert result['on_conflict'] == 'report'
    assert result['conflict']['strict_status'] == 'INFEASIBLE'
    assert result['conflict']['timing']['budget_seconds'] == GenerationOptions().diagnostic_seconds
    assert result['search_stop']['reason'] == 'conflict'
    assert not result.get('diagnostic_proposal')
    assert saved and saved[-1]['strict_conflict']['strict_status'] == 'INFEASIBLE'


def test_continue_flagged_reuses_the_policy_and_keeps_mandatory_breaks(h):
    data = shortfall(h, quota=True)
    data['generation_options'] = {'on_conflict': 'continue_flagged'}
    result = generate(data, 3, deterministic=True)

    assert result['status'] == 'candidate_with_gaps', result
    assert result['on_conflict'] == 'continue_flagged'
    assert result['diagnostic_proposal'] and result['diagnostic_continued']
    assert not result['validation']['valid']
    assert result['conflict']['strict_status'] == 'INFEASIBLE'
    assert any(f['severity'] == 'error' for f in finding_observations(result['validation']))
    assert result['search_stop']['reason'] == 'time_limit' or result['search_stop']['reason'] == 'conflict'
    assert result['breaks']


def test_conflict_decision_contract_supports_exception_and_open_place_choices():
    person = SolverConflictDecision.model_validate({
        'kind': 'person_exception', 'rule_id': 'quota', 'person_id': 'person-1',
        'starts_on': '2026-12-01', 'ends_on': '2027-01-01',
        'person_ids': ['person-1'], 'session_ids': ['session-1'],
        'requires_approval': True})
    leave = SolverConflictDecision.model_validate({
        'kind': 'leave_open', 'session_ids': ['session-1'], 'places': 2,
        'person_ids': [], 'requires_approval': False})
    assert person.person_id == 'person-1' and person.starts_on == '2026-12-01'
    assert leave.places == 2 and leave.session_ids == ['session-1']
    result = JobResult.model_validate({
        'status': 'candidate_with_gaps', 'diagnostic_proposal': True,
        'breaks': [{'rule_id': 'quota', 'session_id': 'session-1', 'person_id': 'person-1'}],
        'search_stop': {'reason': 'conflict', 'search_seconds': 1, 'detail': 'Rules conflict.'}})
    assert result.breaks[0].rule_id == 'quota' and result.search_stop.reason == 'conflict'


def test_conflict_explanation_offers_person_exception_only_for_allowed_rules(h):
    data = shortfall(h, quota=True, people=h.seed['people'][:2], cover_maximum=2)
    data['policies'] = [{**policy, 'definition': {**policy['definition'], 'exception_allowed': True}}
                        if policy['definition']['rule']['kind'] == 'block_quota' else policy
                        for policy in data['policies']]
    quota = next(p for p in data['policies'] if p['definition']['rule']['kind'] == 'block_quota')
    without_quota = copy.deepcopy(data)
    without_quota['policies'] = [p for p in without_quota['policies'] if p['id'] != quota['id']]
    relaxed = generate(without_quota, 3, deterministic=True)
    assert relaxed['status'] == 'candidate_feasible', relaxed
    assert len(relaxed['additions']) == 2
    assert {a['participant']['person_id'] for a in relaxed['additions']} == set(h.seed['people'][:2])
    result = generate(data, 3, deterministic=True)
    decisions = [d for cause in result['conflict']['causes'] for d in cause['decisions']]
    assert result['status'] == 'requirements_conflict'
    assert any(d['kind'] == 'person_exception' and d.get('rule_id') == quota['id']
               and d['person_id'] == h.seed['people'][0] for d in decisions)
    assert not any(d['kind'] == 'person_exception' and d.get('rule_id') == quota['id']
                   and d['person_id'] != h.seed['people'][0] for d in decisions)
    minimum_cover = next(p for p in data['policies'] if p['definition']['rule']['kind'] == 'minimum_cover')
    assert not any(d['kind'] == 'person_exception' and d.get('rule_id') == minimum_cover['id']
                   for d in decisions)
    assert not any(d['kind'] == 'leave_open' for d in decisions)


def test_continue_anyway_switches_a_reported_conflict_to_flagged_mode(h):
    use_cp_sat(h)
    data = shortfall(h, quota=True)
    workspace = h.detail(data['workspace']['id'])['workspace']
    job = h.command('coordinator', 'job.create', {
        'workspace_id': workspace['id'], 'kind': 'generation',
        'generation_options': {'on_conflict': 'report'}}, revision=workspace['revision'])['job']
    from apps.worker.main import process_one
    assert process_one(h.app.state.engine, settings=h.settings)
    reported = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert reported['result']['status'] == 'requirements_conflict'
    assert reported['result']['on_conflict'] == 'report'
    assert reported['result']['resume_available']

    continued = h.command('coordinator', 'job.continue',
                          {'reason': 'Continue with every break flagged'}, job['id'])['job']
    assert continued['effective_options']['on_conflict'] == 'continue_flagged'
    assert continued['diagnostic_proposal']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, continued['id']).resume_state is not None

    assert process_one(h.app.state.engine, settings=h.settings)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, continued['id'])
        assert row.status == 'succeeded'
        assert row.result['status'] == 'candidate_with_gaps'
        assert row.result['diagnostic_proposal']
        assert row.result['validation']['valid'] is False
