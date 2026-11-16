"""Source-specific explanations in the solver summary and saved job API."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.candidate_evidence import candidate_evidence
from packages.domain.cp_sat import generate
from packages.domain.solver_summary import summarise
from packages.domain.validation import evaluate


@pytest.mark.parametrize('problem,code', [
    ('inactive', 'PERSON_INACTIVE'), ('membership', 'MEMBERSHIP_MISSING'),
    ('rank', 'RANK_NOT_ALLOWED'), ('clinic', 'ELIGIBILITY_MISSING'),
    ('leave', 'UNAVAILABLE'), ('programme-restriction', 'RESTRICTION')])
def test_summary_names_the_canonical_candidate_exclusion(h, problem, code):
    policy = h.policy({'kind': 'restriction'}) if problem == 'programme-restriction' else None
    source = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    person = h.seed['people'][0]
    source['people'] = {person: source['people'][person]}
    shift = source['shifts'][0]
    if problem == 'inactive':
        source['people'][person]['active'] = False
        expected = 'marked inactive'
    elif problem == 'membership':
        source['memberships'] = []
        expected = 'without current department membership'
    elif problem == 'rank':
        allowed = h.seed['trainee_category']
        shift['type_snapshot']['definition']['allowed_category_ids'] = [allowed]
        expected = 'without an allowed rank for this duty (allowed: ' + source['categories'][allowed]['name'] + ')'
    elif problem == 'clinic':
        source['eligibilities'] = []
        expected = 'missing clinic eligibility for ' + source['services'][shift['service_ids'][0]]['name']
    elif problem == 'leave':
        source['availability'] = [{'id': 'private-absence', 'person_id': person, 'kind': 'approved_leave',
            'start': shift['start'], 'end': shift['end'], 'private_reason': 'private diagnosis'}]
        expected = 'unavailable'
    else:
        source['programmes'] = {'programme': {'id': 'programme', 'name': 'Academic programme',
            'active': True, 'starts_on': '2020-01-01', 'ends_on': None}}
        source['programme_memberships'] = [{'person_id': person, 'programme_id': 'programme',
            'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}]
        selected = next(p for p in source['policies'] if p['id'] == policy['id'])
        selected['definition']['scope']['programme_id'] = 'programme'
        expected = policy['name']
    evidence = candidate_evidence(source, person, shift)
    assert [r['code'] for r in evidence['exclusions']] == [code]
    summary = summarise(source, evaluate(source), [evidence], {shift['id']})
    if problem in {'inactive', 'membership'}:
        assert summary['top_reasons'] == []
        reason, = summary['excluded_people']
        assert reason['code'] == code and reason['people_count'] == 1
    else:
        reason, = summary['top_reasons']
        assert reason['code'] == code and reason['message'] == expected and reason['session_count'] == 1
    label = 'restricted by ' + expected if policy else expected
    assert summary['open_places'][0]['unusable'] == {label: 1}
    assert label in summary['open_places'][0]['message']
    assert 'private diagnosis' not in str(summary)


def test_same_eligibility_code_keeps_each_clinic_name(h):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    workspace = h.shift(workspace, service=1, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    source = h.snapshot(workspace)
    person = h.seed['people'][0]
    source['people'] = {person: source['people'][person]}
    source['eligibilities'] = []
    evidence = [candidate_evidence(source, person, shift) for shift in source['shifts']]
    summary = summarise(source, evaluate(source), evidence, set(source['focus_ids']))
    assert {r['message'] for r in summary['top_reasons']} == {
        'missing clinic eligibility for ' + source['services'][sid]['name'] for sid in h.seed['services']}
    assert all(r['code'] == 'ELIGIBILITY_MISSING' and r['session_count'] == 1 for r in summary['top_reasons'])


def test_required_capability_shortage_names_the_existing_skill_diagnostic(h):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    source = h.snapshot(workspace)
    capability = h.seed['capability']
    source['shifts'][0]['type_snapshot']['definition']['requirements'][0]['capabilities'] = [
        {'capability_id': capability, 'minimum': 1}]
    source['grants'] = []
    result = generate(source, 5)
    diagnostic = next(d for d in result['diagnostics'] if d['code'] == 'INSUFFICIENT_CAPABILITY_CANDIDATES')
    reason = next(r for r in result['summary']['top_reasons'] if r['code'] == diagnostic['code'])
    assert reason['message'] == diagnostic['message']
    assert source['capabilities'][capability]['name'] in reason['message']
    evidence = [{**row, 'shift_id': diagnostic['shift_id']} for row in diagnostic['candidates']]
    summary = summarise(source, evaluate(source), evidence, set(source['focus_ids']))
    assert summary['top_reasons'][0]['message'] == 'missing required skill: ' + source['capabilities'][capability]['name']
    assert summary['open_places'][0]['unusable'] == {summary['top_reasons'][0]['message']: len(source['people'])}


def test_saved_job_api_preserves_distinct_eligibility_reasons(h):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    source = h.snapshot(workspace)
    clinic, membership, inactive = h.seed['people'][:3]
    source['people'] = {pid: source['people'][pid] for pid in (clinic, membership, inactive)}
    source['eligibilities'] = [r for r in source['eligibilities'] if r['person_id'] != clinic]
    source['memberships'] = [r for r in source['memberships'] if r['person_id'] != membership]
    source['people'][inactive]['active'] = False
    result = generate(source, 5)
    assert result['status'] == 'requirements_conflict'
    with transaction(h.app.state.engine, write=True) as db:
        job = m.Job(department_id=workspace['department_id'], workspace_id=workspace['id'],
            requested_by=h.seed['identities']['coordinator'], kind='generation', status='succeeded',
            input_fingerprint=source['fingerprint'], input_snapshot=source, result=result)
        db.add(job)
        db.flush()
        job_id = job.id
    summary = h.get('coordinator', '/api/jobs/' + job_id)['job']['result']['summary']
    reasons = {r['code']: r['message'] for r in summary['top_reasons']}
    assert reasons['ELIGIBILITY_MISSING'] == 'missing clinic eligibility for ' + source['services'][h.seed['services'][0]]['name']
    assert not {'MEMBERSHIP_MISSING', 'PERSON_INACTIVE'} & reasons.keys()
    assert {row['code']: row['people_count'] for row in summary['excluded_people']} == {
        'MEMBERSHIP_MISSING': 1, 'PERSON_INACTIVE': 1}
