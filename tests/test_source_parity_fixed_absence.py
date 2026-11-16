"""Fixed template placements respect absence without blocking the other sessions."""
import copy
import json

import pytest

from packages.domain.cp_sat import generate as cp_generate
from packages.domain.generation import coverage_findings, generate as greedy_generate
from packages.domain.time import aware
from .test_series_reconciliation import template, materialise, reconcile, amend_template
from .test_source_parity_series import leave


def fixed_template(h):
    duty = h.configure('assignment_types', {'name': 'Synthetic fixed pair', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}]}})['record']
    return template(h, assignment_type_id=duty['id'], days=(0,), definition={
        'anchor': '2026-10-04', 'days': [{'weekday': 0, 'start_time': '08:00', 'end_time': '12:00'}],
        'service_ids': [h.seed['services'][0]], 'default_person_ids': h.seed['people'][:2],
        'usual_assignees_fixed': True, 'close_when_all_usual_unavailable': False})


def absence(h, kind):
    if kind != 'rotation':
        leave(h, start='2026-10-05T08:00:00+03:00',
              end='2026-10-05T10:00:00+03:00' if kind == 'partial' else '2026-10-05T12:00:00+03:00')
        return 'UNAVAILABLE'
    programme = h.configure('programmes', {'name': 'Synthetic absence programme',
        'starts_on': '2020-01-01', 'ends_on': '2030-01-01'})['record']
    cohort = h.configure('cohorts', {'name': 'Synthetic absence cohort', 'programme_id': programme['id'],
        'starts_on': '2020-01-01', 'ends_on': '2030-01-01'})['record']
    h.configure('programme_memberships', {'person_id': h.seed['people'][0], 'programme_id': programme['id'],
        'starts_on': '2026-10-05', 'ends_on': '2026-10-06'})
    h.configure('rotations', {'person_id': h.seed['people'][0], 'cohort_id': cohort['id'],
        'service_id': h.seed['services'][0], 'department_available': False,
        'starts_on': '2026-10-05', 'ends_on': '2026-10-06'})
    return 'EXTERNAL_ROTATION'


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
@pytest.mark.parametrize('kind', ['leave', 'partial', 'rotation'])
@pytest.mark.parametrize('method', ['create', 'expand'])
def test_absent_fixed_usual_is_skipped_with_session_provenance_and_other_rota_continues(h, engine, kind, method):
    source = fixed_template(h)
    code = absence(h, kind)
    if method == 'create':
        reply = h.command('coordinator', 'workspace.create', {'name': 'Synthetic fixed absence period',
            'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'series_ids': [source['id']]})
    else:
        workspace = h.workspace()
        reply = h.command('coordinator', 'series.expand', {'workspace_id': workspace['id'],
            'series_id': source['id']}, revision=workspace['revision'])
    workspace = reply['workspace']
    row = h.detail(workspace)['shifts'][0]
    absent, available = h.seed['people'][:2]
    assert not row['closed'] and not row['cancelled']
    assert [(p['person_id'], p['fixed']) for p in row['participants']] == [(available, True)]
    assert row['series_snapshot']['definition']['default_person_ids'] == [absent, available]
    skipped = row['template_skipped']
    assert len(skipped) == 1
    assert skipped[0]['person_id'] == absent and skipped[0]['code'] == code and skipped[0]['message']
    assert aware(skipped[0]['start']) == aware(row['start']) and aware(skipped[0]['end']) == aware(row['end'])
    assert reply['template_skipped'] == [{'shift_id': row['id'], **skipped[0]}]
    assert row['usual_assignee_unavailability'] == [{'person_id': absent, 'codes': [code],
        'fully_unavailable': kind != 'partial'}]
    assert 'Confidential' not in json.dumps(reply)
    workspace = h.shift(workspace, start='2026-10-06T08:00:00+03:00',
        end='2026-10-06T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(workspace)
    original = copy.deepcopy(data)
    other = next(duty for duty in data['shifts'] if duty['id'] != row['id'])
    result = engine(data, time_budget_seconds=5)
    assert result['status'] == 'candidate_with_gaps', result
    assert any(item['shift_id'] == other['id'] for item in result['additions'])
    assert not any(item['shift_id'] == row['id'] and item['participant']['person_id'] == absent
                   for item in result['additions'])
    assert not result['removals']
    assert any(finding['shift_id'] == row['id'] for finding in coverage_findings(result['validation']))
    assert not any(finding['code'] == code for finding in result['validation']['findings'])
    assert data == original
    assert h.detail(workspace)['shifts'][0]['template_skipped'] == skipped


@pytest.mark.parametrize('participant_policy', ['clear', 'preserve_offsets'])
def test_reconcile_does_not_force_absent_new_usual_but_preserves_reviewed_existing_people(h, participant_policy):
    source = fixed_template(h)
    workspace = materialise(h, source)
    row = h.detail(workspace)['shifts'][0]
    absence(h, 'partial')
    amended = amend_template(h, source, start='08:00', end='12:00', acknowledge_overlaps=True)
    reply = reconcile(h, workspace, amended, replace_modified=True, participant_policy=participant_policy)
    updated = h.detail(reply['workspace'])['shifts'][0]
    if participant_policy == 'clear':
        assert [p['person_id'] for p in updated['participants']] == [h.seed['people'][1]]
        assert updated['template_skipped'][0]['person_id'] == h.seed['people'][0]
        assert reply['template_skipped'] == [{'shift_id': row['id'], **updated['template_skipped'][0]}]
    else:
        assert [p['person_id'] for p in updated['participants']] == [p['person_id'] for p in row['participants']]
        assert updated['template_skipped'] == reply['template_skipped'] == []
        assert any(finding['code'] == 'UNAVAILABLE' for finding in reply['validation']['findings'])


def test_reconcile_skips_absent_usual_added_to_template_and_retains_healthy_usual(h):
    source = fixed_template(h)
    source = amend_template(h, source, start='08:00', end='12:00', default_person_ids=[h.seed['people'][1]])
    workspace = materialise(h, source)
    absence(h, 'leave')
    source = amend_template(h, source, start='08:00', end='12:00', default_person_ids=h.seed['people'][:2], acknowledge_overlaps=True)
    reply = reconcile(h, workspace, source, replace_modified=True)
    row = h.detail(reply['workspace'])['shifts'][0]
    assert [p['person_id'] for p in row['participants']] == [h.seed['people'][1]]
    assert row['template_skipped'][0]['person_id'] == h.seed['people'][0]
    assert reply['template_skipped'] == [{'shift_id': row['id'], **row['template_skipped'][0]}]


def test_absent_occurrence_stays_open_and_does_not_keep_usual_assignment(h):
    source = fixed_template(h)
    source = amend_template(h, source, start='08:00', end='12:00', close_when_all_usual_unavailable=True)
    leave(h)
    leave(h, person=h.seed['people'][1])
    workspace = materialise(h, source)
    row = h.detail(workspace)['shifts'][0]
    # An absence never closes a session; the usual assignments are left open for cover.
    assert not row['closed'] and not row['cancelled']
    assert row['participants'] == []
    assert {row['person_id'] for row in row['template_skipped']} == set(h.seed['people'][:2])


def test_absent_fixed_usual_never_relaxes_a_feasibility_proof(h):
    source = fixed_template(h)
    absence(h, 'leave')
    workspace = materialise(h, source)
    assert not workspace['validation']['valid']
    rejected = h.lifecycle(workspace, 'submit', status=422)
    assert rejected['error']['code'] == 'VALIDATION_FAILED'
    result = cp_generate(h.snapshot(workspace), feasibility_only=True, time_budget_seconds=5)
    assert result['status'] == 'requirements_conflict', result
    assert not result['additions'] and not result['removals']
    assert not result.get('optimality_proven')
