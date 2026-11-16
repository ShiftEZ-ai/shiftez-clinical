"""Staffing mix uses separate scopes; ordered credit is configurable, availability-aware and engine-independent."""
import copy
import time
import uuid
from datetime import timedelta

import pytest

from packages.domain import cp_sat
from packages.domain.recurrence import series_candidate_allowed, with_series_state
from packages.domain.staffing_selection import selected_tier
from packages.domain.time import aware
from packages.domain.validation import evaluate, qualified, staffing_credit
from tests.scheduler_departments import apply, department
from tests.test_source_parity_staffing import cover_world, policy, solve, tree


def world(h, *, period='duty'):
    data = cover_world(h)
    first, second, third = h.seed['people']
    data['policies'] = [policy(data, {'kind': 'minimum_cover', 'minimum': 1,
        'selection_order': [tree('person_ids', first), tree('person_ids', second, third)],
        'selection_period': period})]
    return data


def count(data, people):
    shift, configured = data['shifts'][0], data['policies'][0]
    return staffing_credit(data, configured, shift, aware(shift['start']), aware(shift['end']), dict.fromkeys(people, 1))


@pytest.mark.parametrize('engine', ['cp_sat', 'greedy'])
def test_engines_fill_the_first_available_configured_population_and_a_disjoint_mix(h, engine):
    data = world(h)
    first, second, third = h.seed['people']
    # One ordered population and one independent population make a mixed pair without another rule concept.
    data['policies'][0]['definition']['scope'] = {'person_ids': [first, second]}
    data['policies'].append(policy(data, {'kind': 'minimum_cover', 'minimum': 1}, {'person_id': third}))
    result = solve(data, engine)
    assert result['status'] == 'candidate_feasible', result
    assert {row['participant']['person_id'] for row in result['additions']} == {first, third}
    assert not [f for f in evaluate(apply(data, result))['findings'] if f['code'] == 'SCOPED_COVERAGE_GAP']


def test_omitting_an_available_person_cannot_change_the_selected_tier(h):
    data = world(h)
    first, second, _ = h.seed['people']
    assert count(data, [second]) == {}
    assert set(count(data, [first, second])) == {first}
    data['policies'][0]['definition']['scope'] = {'person_ids': h.seed['people'][1:]}
    assert set(count(data, [first, second])) == {second}


@pytest.mark.parametrize('block', ['leave', 'external_commitment', 'permission', 'rotation', 'restriction', 'inactive'])
@pytest.mark.parametrize('engine', ['cp_sat', 'greedy'])
def test_structural_availability_controls_fallback_in_both_engines(h, block, engine):
    data = world(h)
    first, *others = h.seed['people']
    if block in ('leave', 'external_commitment'):
        data['availability'].append({'id': 'synthetic-ordered-absence', 'person_id': first,
            'kind': 'recorded_leave' if block == 'leave' else block,
            'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00'})
    elif block == 'permission':
        data['eligibilities'] = [row for row in data['eligibilities'] if row['person_id'] != first]
    elif block == 'rotation':
        data['rotations'].append({'id': 'synthetic-ordered-rotation', 'person_id': first,
            'department_available': False, 'starts_on': '2026-10-04', 'ends_on': '2026-10-05'})
    elif block == 'restriction':
        data['policies'].append(policy(data, {'kind': 'restriction'}, {'person_id': first}))
    else:
        data['people'][first]['active'] = False
    assert set(count(data, h.seed['people'])) == set(others)
    result = solve(data, engine)
    assert result['status'] == 'candidate_feasible', result
    assert len(result['additions']) == 1 and result['additions'][0]['participant']['person_id'] in others


@pytest.mark.parametrize('fixed', [False, True])
@pytest.mark.parametrize('engine', ['cp_sat', 'greedy'])
@pytest.mark.parametrize('period', ['duty', 'week'])
def test_ordered_tiers_and_engines_agree_on_fixed_usual_eligibility(h, fixed, engine, period):
    data = world(h, period=period)
    outsider, usual, _ = h.seed['people']
    shift, configured = data['shifts'][0], data['policies'][0]
    shift['series_snapshot'] = {'definition': {'default_person_ids': [usual], 'usual_assignees_fixed': fixed}}
    configured['definition']['rule']['selection_order'] = [tree('person_ids', outsider), tree('person_ids', usual)]
    a, b = aware(shift['start']), aware(shift['end'])
    assert qualified(data, outsider, shift, shift['type_snapshot']['definition'], a, b)
    assert series_candidate_allowed(data, shift, outsider) is (not fixed)
    expected = usual if fixed else outsider
    assert selected_tier(data, configured, shift, a, b) == (1 if fixed else 0)
    assert set(count(data, [outsider, usual])) == {expected}
    result = solve(data, engine)
    assert result['status'] == 'candidate_feasible', result
    assert [row['participant']['person_id'] for row in result['additions']] == [expected]
    assert evaluate(apply(data, result))['valid']


@pytest.mark.parametrize('engine', ['cp_sat', 'greedy'])
@pytest.mark.parametrize('period', ['duty', 'week'])
def test_ordered_tiers_do_not_substitute_outsiders_when_usual_absence_leaves_an_open_gap(h, engine, period):
    data = world(h, period=period)
    outsider, usual, _ = h.seed['people']
    shift = data['shifts'][0]
    shift['series_snapshot'] = {'definition': {'default_person_ids': [usual], 'usual_assignees_fixed': True,
                                               'close_when_all_usual_unavailable': True}}
    data['policies'][0]['definition']['rule']['selection_order'] = [tree('person_ids', outsider), tree('person_ids', usual)]
    data['availability'].append({'id': 'synthetic-usual-absence', 'person_id': usual, 'kind': 'recorded_leave',
                                  'start': shift['start'], 'end': shift['end']})
    projected = with_series_state(data)
    open_shift = projected['shifts'][0]
    assert not open_shift['closed'] and open_shift['closure_reason'] is None
    assert selected_tier(projected, data['policies'][0], open_shift, aware(open_shift['start']), aware(open_shift['end'])) == -1
    result = solve(data, engine)
    assert result['status'] == 'candidate_with_gaps', result
    assert result['additions'] == result['removals'] == []
    checked = evaluate(apply(data, result))
    assert not checked['valid'] and [row['status'] for row in checked['coverage']] == ['gap']


def test_weekly_tier_checks_each_opportunity_usual_pool_before_caching(h):
    data = world(h, period='week')
    first, second, _ = h.seed['people']
    shift = data['shifts'][0]
    shift['series_snapshot'] = {'definition': {'default_person_ids': [second], 'usual_assignees_fixed': True}}
    peer = copy.deepcopy(shift)
    peer.update(id=str(uuid.uuid4()), start=(aware(shift['start']) + timedelta(days=1)).isoformat(),
                end=(aware(shift['end']) + timedelta(days=1)).isoformat(),
                series_snapshot={'definition': {'default_person_ids': [first], 'usual_assignees_fixed': True}})
    data['shifts'].append(peer)
    data['_staffing_selection_cache'] = {}
    for row in (shift, peer):
        assert selected_tier(data, data['policies'][0], row, aware(row['start']), aware(row['end'])) == 0


def test_ordered_tiers_keep_the_canonical_trainee_exception_to_fixed_usual_people(h):
    data = world(h)
    outsider, usual, trainee = h.seed['people']
    shift = data['shifts'][0]
    shift['series_snapshot'] = {'definition': {'default_person_ids': [usual], 'usual_assignees_fixed': True}}
    shift['type_snapshot']['definition']['supervision'] = {'trainee_category_ids': [h.seed['trainee_category']],
        'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1, 'require_pairing': True}
    data['memberships'].append({'person_id': trainee, 'category_id': h.seed['trainee_category'],
                               'department_id': data['workspace']['department_id'],
                               'starts_on': data['workspace']['starts_on'], 'ends_on': None})
    data['policies'][0]['definition']['rule']['selection_order'] = [tree('person_ids', outsider), tree('person_ids', trainee)]
    assert series_candidate_allowed(data, shift, trainee)
    assert set(count(data, [outsider, trainee])) == {trainee}


def test_week_selection_uses_configured_calendar_and_does_not_claim_local_fallback(h):
    data = world(h)
    first, second, _ = h.seed['people']
    shift = data['shifts'][0]
    peer = copy.deepcopy(shift)
    shift.update(start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')
    peer['id'] = str(uuid.uuid4())
    data['shifts'].append(peer)
    data['availability'].append({'id': 'synthetic-ordered-week-absence', 'person_id': first, 'kind': 'recorded_leave',
        'start': '2026-10-05T00:00:00+03:00', 'end': '2026-10-06T00:00:00+03:00'})
    assert set(count(data, [second])) == {second}  # Duty-specific fallback.
    data['policies'][0]['definition']['rule']['selection_period'] = 'week'
    assert count(data, [second]) == {}  # The higher tier is available on another duty in this same week.
    data['organisation']['week_start'] = 0
    assert set(count(data, [second])) == {second}  # Sunday now belongs to the preceding week.


@pytest.mark.parametrize('engine', ['cp_sat', 'greedy'])
@pytest.mark.parametrize('period', ['duty', 'week'])
def test_engines_keep_the_configured_weekly_tier_when_a_later_duty_has_a_gap(h, engine, period):
    data = world(h, period=period)
    first = h.seed['people'][0]
    shift = data['shifts'][0]
    peer = copy.deepcopy(shift)
    shift.update(start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')
    peer['id'] = str(uuid.uuid4())
    data['shifts'].append(peer)
    data['focus_ids'].append(peer['id'])
    data['availability'].append({'id': 'synthetic-ordered-week-absence', 'person_id': first, 'kind': 'recorded_leave',
        'start': '2026-10-05T00:00:00+03:00', 'end': '2026-10-06T00:00:00+03:00'})
    result = solve(data, engine)
    assert (result['status'] == 'candidate_feasible') == (period == 'duty'), result
    if period == 'duty':
        assert len(result['additions']) == 2


def test_week_selection_uses_matching_clocks_and_dated_person_facts(h):
    data = world(h, period='week')
    first, second, _ = h.seed['people']
    shift = data['shifts'][0]
    peer = copy.deepcopy(shift)
    shift.update(start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00')
    peer['id'] = str(uuid.uuid4())
    data['shifts'].append(peer)
    data['policies'][0]['definition']['scope'] = {'start_time': '08:00', 'end_time': '10:00'}
    data['availability'].extend([
        {'id': 'synthetic-ordered-absence-a', 'person_id': first, 'kind': 'recorded_leave',
         'start': '2026-10-04T10:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00'},
        {'id': 'synthetic-ordered-absence-b', 'person_id': first, 'kind': 'recorded_leave',
         'start': '2026-10-06T00:00:00+03:00', 'end': '2026-10-07T00:00:00+03:00'},
    ])
    configured = data['policies'][0]
    assert selected_tier(data, configured, shift, aware(shift['start']), aware('2026-10-06T10:00:00+03:00')) == 0
    for row in data['memberships']:
        if row['person_id'] == first:
            row['starts_on'] = '2026-10-05'
    assert selected_tier(data, configured, shift, aware(shift['start']), aware('2026-10-06T10:00:00+03:00')) == 1


def test_conditional_companions_must_belong_to_the_selected_tier(h):
    data = world(h)
    first, second, third = h.seed['people']
    rule = data['policies'][0]['definition']['rule']
    rule.update(selection_order=[tree('person_ids', first, second), tree('person_ids', third)],
                credit_conditions=tree('person_ids', first), credit_minimum_others=2)
    assert set(count(data, h.seed['people'])) == {second}


def test_competing_assignments_do_not_manipulate_structural_selection(h):
    data = world(h)
    first, second, _ = h.seed['people']
    shift = data['shifts'][0]
    peer = copy.deepcopy(shift)
    peer.update(id=shift['id'] + '-competing', participants=[{'person_id': first,
                'start': shift['start'], 'end': shift['end'], 'overtime': False}])
    data['shifts'].append(peer)
    assert count(data, [second]) == {}


@pytest.fixture(scope='module', params=['duty', 'week'])
def department_ordered_selection(scale_harness, request):
    h, period = scale_harness('h'), request.param
    data, _ = department(h, 11, people=150, days=28, clinics=9, rules='none', leave_rate=0)
    people = list(data['people'])
    data['policies'] = [policy(data, {'kind': 'minimum_cover', 'minimum': 1,
        'selection_order': [tree('person_ids', *people[:75]), tree('person_ids', *people[75:])],
        'selection_period': period})]
    captured = []
    started = time.process_time()
    result = cp_sat.generate(data, 5, workers=1, deterministic=True, feasibility_only=True, _on_model=captured.append)
    elapsed = time.process_time() - started
    # The compiler must keep the broad candidate pool; selected tier changes credit, not who may work.
    candidate_variables = [var for var in captured[0].proto.variables if var.name.count(':') == 1]
    print('ordered staffing selection', {'period': period, 'people': 150, 'sessions': len(data['shifts']),
                                       'cpu_seconds': round(elapsed, 3), 'candidate_variables': len(candidate_variables),
                                       'variables': len(captured[0].proto.variables), 'constraints': len(captured[0].proto.constraints)})
    return data, result, candidate_variables, elapsed


def test_ordered_selection_has_a_full_candidate_department_scale_cpu_budget(department_ordered_selection):
    data, result, candidate_variables, _ = department_ordered_selection
    assert result['status'] == 'model_ready', result
    assert len(data['people']) == 150 and len(data['shifts']) >= 500
    assert len(candidate_variables) >= 50_000


@pytest.mark.cpu_budget
def test_ordered_selection_department_scale_cpu_budget(department_ordered_selection):
    from tests.test_scheduler_performance import _budget
    elapsed = department_ordered_selection[3]
    assert elapsed < _budget(90), elapsed
