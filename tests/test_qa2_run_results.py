"""Acceptance regressions for run explanations, lightweight history and shown-score changes."""
import copy
import uuid

import pytest
from sqlalchemy import insert

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.candidate_evidence import candidate_evidence, direct_coverage_shortages
from packages.domain.cp_sat import generate
from packages.domain.solver_summary import summarise
from packages.domain.validation import evaluate


def test_proven_unfilled_places_outrank_broad_people_exclusions(h):
    workspace = h.workspace()
    for day in range(4, 9):
        workspace = h.shift(workspace, start=f'2026-10-{day:02}T08:00:00+03:00',
                            end=f'2026-10-{day:02}T12:00:00+03:00', people=[], service=int(day == 8))['workspace']
    source = h.snapshot(workspace)
    inactive, nonmember, unavailable = h.seed['people'][:3]
    flexible = str(uuid.uuid5(uuid.NAMESPACE_URL, 'qa2-synthetic-flexible-person'))
    source['people'][flexible] = {**source['people'][inactive], 'id': flexible}
    for key in ('memberships', 'eligibilities', 'grants'):
        source[key].extend({**row, 'id': flexible + row['id'], 'person_id': flexible}
                           for row in list(source[key]) if row['person_id'] == inactive)
    source['people'][inactive]['active'] = False
    source['memberships'] = [row for row in source['memberships'] if row['person_id'] != nonmember]
    source['eligibilities'] = [row for row in source['eligibilities'] if row['service_id'] != h.seed['services'][1]]
    source['availability'] = [{'id': 'synthetic-absence', 'person_id': unavailable,
                               'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-09T00:00:00+03:00'}]
    special = source['shifts'][-1]
    special['type_snapshot'] = copy.deepcopy(special['type_snapshot'])
    special['type_snapshot']['definition']['requirements'][0]['minimum'] = 8
    evidence = [candidate_evidence(source, pid, shift) for shift in source['shifts'] for pid in source['people']]
    diagnostics = direct_coverage_shortages(source, evidence, set(source['focus_ids']))
    summary = summarise(source, evaluate(source), evidence, set(source['focus_ids']),
                        diagnostics=diagnostics, explain_limit=0)
    first = summary['top_reasons'][0]
    assert first['code'] == 'INSUFFICIENT_WHOLE_DUTY_CANDIDATES'
    assert first['session_count'] == 1 and first['places_count'] == 8
    assert not {'PERSON_INACTIVE', 'MEMBERSHIP_MISSING'} & {row['code'] for row in summary['top_reasons']}
    assert {row['code']: row['people_count'] for row in summary['excluded_people']} == {
        'PERSON_INACTIVE': 1, 'MEMBERSHIP_MISSING': 1}
    assert '1 person excluded: no department membership' in {row['message'] for row in summary['excluded_people']}
    # Reasons for a filled session cannot explain the remaining open places.
    filled = source['shifts'][0]
    filled['participants'] = [{'id': 'synthetic-assignment', 'person_id': flexible,
                              'start': filled['start'], 'end': filled['end'], 'participation': 'staff',
                              'overtime': False, 'revision': 1}]
    summary = summarise(source, evaluate(source), evidence, set(source['focus_ids']),
                        diagnostics=diagnostics, explain_limit=0)
    reason = next(row for row in summary['top_reasons'] if row['code'] == 'UNAVAILABLE')
    assert reason['session_count'] == 4


def check_history_outcomes(h):
    workspace = h.workspace()
    source = h.snapshot(workspace)
    cases = [
        ('succeeded', {'status': 'candidate_feasible', 'validation': {'valid': True}}, 'complete'),
        ('succeeded', {'status': 'candidate_with_gaps', 'validation': {'valid': False, 'error_count': 2},
                       'summary': {'required_places': 8, 'filled_places': 6, 'open_places_count': 2}}, 'partial'),
        ('succeeded', {'status': 'requirements_conflict'}, 'infeasible'),
        ('succeeded', {'status': 'unknown_time_limit'}, 'needs_more_time'),
        ('succeeded', {'status': 'unknown_time_limit', 'validation': {'valid': True}}, 'complete'),
        ('succeeded', {'status': 'translation_error'}, 'failed'),
        ('failed', None, 'failed'), ('cancelled', None, 'cancelled'),
        ('applied', {'status': 'candidate_with_gaps'}, 'partial'), ('queued', None, None),
    ]
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Job), [{'id': f'outcome-{n}', 'department_id': workspace['department_id'],
            'workspace_id': workspace['id'], 'requested_by': h.seed['identities']['coordinator'],
            'kind': 'generation', 'status': status, 'input_snapshot': source,
            'input_fingerprint': source['fingerprint'], 'result': result} for n, (status, result, _) in enumerate(cases)])
    listed = {row['id']: row for row in h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs']}
    for n, (status, result, outcome) in enumerate(cases):
        row = listed[f'outcome-{n}']
        detail = h.get('coordinator', '/api/jobs/' + row['id'])['job']
        assert row['status'] == detail['status'] == status
        assert row['outcome'] == detail['outcome'] == outcome
        assert (row['result'] or {}).get('status') == (result or {}).get('status')
    assert listed['outcome-1']['result']['summary']['open_places_count'] == 2


def test_membership_missing_for_only_one_session_is_not_a_global_exclusion(h):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    source = h.snapshot(workspace)
    person = h.seed['people'][0]
    source['people'] = {person: source['people'][person]}
    for membership in source['memberships']:
        if membership['person_id'] == person:
            membership['starts_on'] = '2026-10-05'
    evidence = [candidate_evidence(source, person, shift) for shift in source['shifts']]
    summary = summarise(source, evaluate(source), evidence, set(source['focus_ids']), explain_limit=0)
    reason, = summary['top_reasons']
    assert reason['code'] == 'MEMBERSHIP_MISSING' and reason['session_count'] == 1
    assert summary['excluded_people'] == []


def test_job_history_outcomes_match_detail(h):
    check_history_outcomes(h)


@pytest.mark.postgres
def test_job_history_outcomes_match_detail_postgres(pg):
    check_history_outcomes(pg)


@pytest.mark.parametrize('partial', [False, True])
def test_score_delta_uses_uncapped_before_and_after(h, partial):
    workspace = h.workspace()
    for day in range(4, 9):
        workspace = h.shift(workspace, start=f'2026-10-{day:02}T08:00:00+03:00',
                            end=f'2026-10-{day:02}T12:00:00+03:00',
                            people=[] if day == 8 else [h.seed['people'][0]])['workspace']
    source = h.snapshot(workspace)
    if partial:
        source['people'] = {h.seed['people'][0]: source['people'][h.seed['people'][0]]}
        source['shifts'][-1]['type_snapshot'] = copy.deepcopy(source['shifts'][-1]['type_snapshot'])
        source['shifts'][-1]['type_snapshot']['definition']['requirements'][0]['minimum'] = 2
    result = generate(copy.deepcopy(source), 5, diagnostic_proposal=partial)
    assert result['status'] == ('candidate_with_gaps' if partial else 'candidate_feasible')
    summary = result['summary']
    before, after = summary['score_before'], summary['score_saved']
    assert not before['publishable'] and after['publishable'] is not partial
    assert summary['score_delta'] == round(after['uncapped_score'] - before['uncapped_score'], 1) > 0
    assert summary['score_delta'] != round(after['score'] - before['score'], 1)
