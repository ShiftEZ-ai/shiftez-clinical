"""Frozen recurring assignees and explicit absence-derived closure."""
import pytest
import time

from packages.domain.cp_sat import generate as cp_generate
from packages.domain.generation import generate as greedy_generate
from packages.domain.recurrence import usual_assignee_ids, with_series_state, series_candidate_allowed
from packages.domain.reports import workload
from packages.domain.validation import evaluate
from .test_series_reconciliation import template, materialise, reconcile, amend_template


def series(h, **options):
    return template(h, days=(0,), definition={'anchor': '2026-10-04',
        'days': [{'weekday': 0, 'start_time': '08:00', 'end_time': '12:00'}],
        'service_ids': [h.seed['services'][0]], 'default_person_ids': [h.seed['people'][0]], **options})


def leave(h, start='2026-10-05T00:00:00+03:00', end='2026-10-06T00:00:00+03:00', person=None):
    return h.command('coordinator', 'availability.record', {'person_id': person or h.seed['people'][0],
        'kind': 'recorded_leave', 'start': start, 'end': end, 'private_reason': 'Confidential synthetic leave'})['record']


def test_default_series_behavior_and_frozen_usual_identity_are_preserved(h):
    source = series(h, usual_assignees_fixed=False)
    leave(h)
    workspace = materialise(h, source)
    row = h.detail(workspace)['shifts'][0]
    assert not row['participants'] and row['template_skipped'][0]['code'] == 'UNAVAILABLE'
    assert not row.get('closed') and not row['cancelled']
    assert usual_assignee_ids(row) == frozenset(source['definition']['default_person_ids'])
    assert row['series_snapshot']['revision'] == source['revision']
    amended = amend_template(h, source, default_person_ids=[h.seed['people'][1]])
    assert h.detail(workspace)['shifts'][0]['series_snapshot'] == row['series_snapshot']
    result = reconcile(h, workspace, amended)
    assert h.detail(result['workspace'])['shifts'][0]['series_snapshot']['revision'] == amended['revision']


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
@pytest.mark.parametrize('mode', ['fill_gaps', 'rebalance'])
def test_configured_usual_people_stay_fixed_in_every_generation_mode(h, engine, mode):
    source = series(h, usual_assignees_fixed=True)
    workspace = materialise(h, source)
    data = h.snapshot(workspace)
    row = data['shifts'][0]
    assert row['participants'][0]['fixed'] and not row['series_modified']
    data['generation_options'] = {'mode': mode}
    result = engine(data)
    assert result['status'] == 'candidate_feasible', result
    assert result['removals'] == result['additions'] == []


@pytest.mark.parametrize('fixed', [False, True])
def test_full_absence_leaves_an_open_place_and_reopens_with_the_person(h, fixed):
    source = series(h, usual_assignees_fixed=fixed, close_when_all_usual_unavailable=True)
    absence = leave(h)
    workspace = materialise(h, source)
    row = h.detail(workspace)['shifts'][0]
    assert not row['closed'] and not row['cancelled'] and row['cancellation_reason'] is None
    assert row['closure_reason'] is None
    assert row['usual_assignee_unavailability'] == [{'person_id': h.seed['people'][0],
        'codes': ['UNAVAILABLE'], 'fully_unavailable': True}]
    assert row['participants'] == []
    assert 'Confidential' not in str(row)
    data = h.snapshot(workspace)
    check = evaluate(data)
    assert [r['status'] for r in check['coverage']] == ['gap']
    report = workload({**data, 'timezone': data['organisation']['timezone'],
        'input_revision': data['organisation']['input_revision'],
        'availability_revision': data['organisation']['availability_revision']})
    assert report['rows'] == []
    for engine in (greedy_generate, cp_generate):
        result = engine(data)
        assert result['status'] == ('candidate_with_gaps' if fixed else 'candidate_feasible'), result
        assert result['removals'] == []
        assert bool(result['additions']) is (not fixed)
    h.command('coordinator', 'availability.retract', {'reason': 'Synthetic retraction'}, absence['id'], absence['revision'])
    opened = h.detail(workspace)['shifts'][0]
    assert not opened['closed'] and not opened['cancelled']
    assert opened['participants'] == []


def test_partial_absence_remains_a_visible_conflict_and_does_not_close(h):
    source = series(h, usual_assignees_fixed=True, close_when_all_usual_unavailable=True)
    workspace = materialise(h, source)
    leave(h, start='2026-10-05T08:00:00+03:00', end='2026-10-05T10:00:00+03:00')
    row = h.detail(workspace)['shifts'][0]
    assert not row['closed'] and row['participants']
    assert not row['usual_assignee_unavailability'][0]['fully_unavailable']
    assert 'UNAVAILABLE' in {f['code'] for f in evaluate(h.snapshot(workspace))['findings']}


def test_retired_closure_setting_leaves_place_open_until_absence_is_retracted(h):
    source = series(h, usual_assignees_fixed=True, close_when_all_usual_unavailable=True)
    workspace = materialise(h, source)
    absence = leave(h)
    assert h.lifecycle(workspace, 'submit', status=422)['error']['code'] == 'VALIDATION_FAILED'
    h.command('coordinator', 'availability.retract', {'reason': 'Synthetic retraction'}, absence['id'], absence['revision'])
    publication = h.publish(workspace)
    before = h.detail(workspace)['shifts'][0]
    assert not before['closed'] and before['series_state_frozen']
    amendment = h.command('coordinator', 'workspace.create', {'name': 'Fresh closure review',
        'starts_on': workspace['starts_on'], 'ends_on': workspace['ends_on'],
        'base_publication_id': publication['publication_id']})['workspace']
    copied = h.detail(amendment)['shifts'][0]
    assert not copied['closed'] and not copied.get('series_state_frozen') and not copied['cancelled']
    assert copied['series_snapshot'] == before['series_snapshot']


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
def test_fixed_usual_clinic_allows_dynamic_trainees_but_no_substitute_clinician(h, engine):
    usual, alternative, trainee = h.seed['people']
    h.command('coordinator', 'membership.create', {'person_id': trainee,
        'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    duty = h.configure('assignment_types', {'name': 'Supervised usual clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}],
            'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
                'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1, 'require_pairing': True}}})['record']
    h.policy({'kind': 'allocation_preference', 'priority': 100}, {'person_id': alternative}, 'advisory')
    source = template(h, days=(0,), assignment_type_id=duty['id'], definition={'anchor': '2026-10-04',
        'days': [{'weekday': 0, 'start_time': '08:00', 'end_time': '12:00'}],
        'service_ids': [h.seed['services'][0]], 'default_person_ids': [usual], 'usual_assignees_fixed': True})
    workspace = materialise(h, source)
    data = h.snapshot(workspace)
    data['generation_options'] = {'mode': 'rebalance'}
    result = engine(data)
    assert result['status'] == 'candidate_feasible', result
    assert [row['participant']['person_id'] for row in result['additions']] == [trainee]
    assert result['removals'] == []


@pytest.fixture(scope='module')
def department_series_state():
    people = [str(pid) for pid in range(150)]
    data = {'organisation': {'timezone': 'UTC'}, 'availability': [{'person_id': pid,
        'start': '2026-10-05T00:00:00+00:00', 'end': '2026-10-06T00:00:00+00:00'} for pid in people for _ in range(50)],
        'rotations': [], 'shifts': [{'id': str(sid), 'cancelled': False,
            'start': '2026-10-05T08:00:00+00:00', 'end': '2026-10-05T12:00:00+00:00',
            'type_snapshot': {'definition': {'supervision': None}},
            'series_snapshot': {'definition': {'default_person_ids': [people[(sid + offset) % 150] for offset in range(3)],
                'usual_assignees_fixed': True, 'close_when_all_usual_unavailable': True}}} for sid in range(500)]}
    started = time.process_time()
    projected = with_series_state(data)
    allowed = sum(series_candidate_allowed(data, row, pid) for row in data['shifts'] for pid in people)
    elapsed = time.process_time() - started
    print('series state and candidate policy', {'cpu_seconds': elapsed, 'people': 150, 'duties': 500,
        'absence_rows': len(data['availability']), 'candidate_checks': 75000})
    return data, projected, allowed, elapsed


def test_department_scale_series_state_is_bounded_and_leaves_source_intact(department_series_state):
    data, projected, allowed, _ = department_series_state
    assert all(not row['closed'] and not row['cancelled'] for row in projected['shifts'])
    assert all('closed' not in row for row in data['shifts'])
    assert allowed == 1500


@pytest.mark.cpu_budget
def test_department_scale_series_state_cpu_budget(department_series_state):
    from tests.test_scheduler_performance import _budget
    elapsed = department_series_state[3]
    assert elapsed < _budget(4), elapsed
