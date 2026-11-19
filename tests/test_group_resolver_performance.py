"""Group responsibility resolution equivalence and department-scale budgets."""
from time import perf_counter, process_time

import pytest

from packages.domain.planning_groups import GroupResolver
from tests.test_performance_budget import python_budget


def _group_data():
    people = {f'p{n}': {'id': f'p{n}'} for n in range(163)}
    shifts = []
    for n in range(768):
        day, hour = 1 + n // 27, 8 + n % 3
        shifts.append({'id': f's{n}', 'start': f'2026-10-{day:02}T{hour:02}:00:00+00:00',
                       'end': f'2026-10-{day:02}T{hour + 1:02}:00:00+00:00'})
    data = {'organisation': {'timezone': 'UTC'}, 'people': people, 'shifts': shifts}
    group = {'group_id': 'g', 'id': 'v', 'revision': 1, 'definition': {
        'name': 'Configured population', 'starts_on': '2026-10-01', 'ends_on': None, 'active': True,
        'membership': {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': list(people)}]},
        'owner_identity_ids': ['owner'], 'co_editor_identity_ids': [], 'primary_priority': 10}}
    return data, [group]


def _measure(data, groups, *, fast):
    resolver = GroupResolver(data, groups)
    resolver.day_fast_path = fast
    wall_started, cpu_started = perf_counter(), process_time()
    resolved = sum(resolver.primary(person_id, shift) is not None
                   for shift in data['shifts'] for person_id in data['people'])
    return {'resolved': resolved, 'wall_seconds': perf_counter() - wall_started,
            'cpu_seconds': process_time() - cpu_started,
            'day_cache': len(getattr(resolver, 'day_cache', {})), 'person_segments': len(getattr(resolver, 'person_segments', {}))}


@pytest.fixture(scope='module')
def department_scale_measurement():
    data, groups = _group_data()
    before = _measure(data, groups, fast=False)
    after = _measure(data, groups, fast=True)
    print('group resolver department scale', {'people': 163, 'sessions': 768,
                                               'before': before, 'after': after})
    return before, after


def test_group_resolver_department_scale_equivalence(department_scale_measurement):
    before, after = department_scale_measurement
    assert before['resolved'] == after['resolved'] == 163 * 768
    assert after['person_segments'] == 163
    assert after['day_cache'] > 0


@pytest.mark.wall_benchmark
def test_group_resolver_department_scale_wall_budget(department_scale_measurement):
    before, after = department_scale_measurement
    budget = python_budget(1.0)
    assert before['wall_seconds'] > budget, (before, budget)
    assert after['wall_seconds'] < budget, (after, budget)


@pytest.mark.cpu_budget
def test_group_resolver_department_scale_cpu_budget(department_scale_measurement):
    _, after = department_scale_measurement
    assert after['cpu_seconds'] < python_budget(1.0), after


def test_group_resolver_cached_primary_matches_uncached_across_boundaries():
    data = {'organisation': {'timezone': 'UTC'},
            'people': {pid: {'id': pid} for pid in ('p1', 'p2', 'p3')},
            'shifts': []}
    def membership(*people):
        return {'match': 'all', 'conditions': [
            {'field': 'person_ids', 'op': 'any', 'values': list(people)}]}
    groups = [
        {'group_id': 'g1', 'id': 'v1', 'revision': 1, 'definition': {
            'name': 'First version', 'starts_on': '2026-10-01', 'ends_on': '2026-10-05', 'active': True,
            'membership': membership('p1'), 'owner_identity_ids': ['owner-1'],
            'co_editor_identity_ids': [], 'primary_priority': 10}},
        {'group_id': 'g1', 'id': 'v2', 'revision': 2, 'definition': {
            'name': 'Second version', 'starts_on': '2026-10-05', 'ends_on': None, 'active': True,
            'membership': membership('p1'), 'owner_identity_ids': ['owner-2'],
            'co_editor_identity_ids': [], 'primary_priority': 10}},
        {'group_id': 'g2', 'id': 'v3', 'revision': 1, 'definition': {
            'name': 'Ended membership', 'starts_on': '2026-10-01', 'ends_on': '2026-10-05', 'active': True,
            'membership': membership('p3'), 'owner_identity_ids': ['owner-3'],
            'co_editor_identity_ids': [], 'primary_priority': 20}},
    ]
    shifts = {
        'before_end': {'id': 'before-end', 'start': '2026-10-04T08:00:00+00:00', 'end': '2026-10-04T12:00:00+00:00'},
        'after_start': {'id': 'after-start', 'start': '2026-10-05T08:00:00+00:00', 'end': '2026-10-05T12:00:00+00:00'},
        'crossing_boundary': {'id': 'crossing', 'start': '2026-10-04T23:00:00+00:00', 'end': '2026-10-05T01:00:00+00:00'},
    }
    cached, uncached = GroupResolver(data, groups), GroupResolver(data, groups)
    uncached.day_fast_path = False
    for person_id, shift in (('p1', shifts['before_end']), ('p1', shifts['after_start']),
                             ('p1', shifts['crossing_boundary']), ('p2', shifts['before_end']),
                             ('p3', shifts['before_end']), ('p3', shifts['after_start'])):
        assert cached.primary(person_id, shift) == uncached.primary(person_id, shift)
    data = {'organisation': {'timezone': 'UTC'},
            'people': {pid: {'id': pid} for pid in ('p1', 'p2')}, 'shifts': []}
    overnight = {'id': 'overnight', 'start': '2026-10-04T20:00:00+00:00',
                 'end': '2026-10-05T08:00:00+00:00'}

    def membership(*people):
        return {'match': 'all', 'conditions': [
            {'field': 'person_ids', 'op': 'any', 'values': list(people)}]}

    cases = [
        # p1 changes from g-end to g-next exactly at the local midnight boundary.
        [
            {'group_id': 'g-end', 'id': 'v-end', 'revision': 1, 'definition': {
                'name': 'Ends at midnight', 'starts_on': '2026-10-01', 'ends_on': '2026-10-05', 'active': True,
                'membership': membership('p1'), 'owner_identity_ids': ['owner-end'],
                'co_editor_identity_ids': [], 'primary_priority': 10}},
            {'group_id': 'g-next', 'id': 'v-next', 'revision': 1, 'definition': {
                'name': 'Starts after end', 'starts_on': '2026-10-05', 'ends_on': None, 'active': True,
                'membership': membership('p1'), 'owner_identity_ids': ['owner-next'],
                'co_editor_identity_ids': [], 'primary_priority': 10}},
        ],
        # p1 has no group before midnight and starts membership exactly at midnight.
        [
            {'group_id': 'g-other', 'id': 'v-other', 'revision': 1, 'definition': {
                'name': 'Other person before start', 'starts_on': '2026-10-01', 'ends_on': '2026-10-05', 'active': True,
                'membership': membership('p2'), 'owner_identity_ids': ['owner-other'],
                'co_editor_identity_ids': [], 'primary_priority': 10}},
            {'group_id': 'g-start', 'id': 'v-start', 'revision': 1, 'definition': {
                'name': 'Starts at midnight', 'starts_on': '2026-10-05', 'ends_on': None, 'active': True,
                'membership': membership('p1'), 'owner_identity_ids': ['owner-start'],
                'co_editor_identity_ids': [], 'primary_priority': 10}},
        ],
    ]
    for groups in cases:
        cached, uncached = GroupResolver(data, groups), GroupResolver(data, groups)
        uncached.day_fast_path = False
        assert cached.primary('p1', overnight) is None
        assert cached.primary('p1', overnight) == uncached.primary('p1', overnight)
    last_day = {'id': 'last-day', 'start': '9999-12-31T08:00:00+00:00',
                'end': '9999-12-31T12:00:00+00:00'}
    assert cached.primary('p1', last_day) == uncached.primary('p1', last_day)
    across_last_day = {**last_day, 'start': '9999-12-30T08:00:00+00:00'}
    assert cached.primary('p1', across_last_day) == uncached.primary('p1', across_last_day)


def test_group_resolver_empty_projection_snapshot_keeps_fast_exact_parity():
    """The no-group fix-list projection intentionally omits organisation metadata."""
    data = {'shifts': [], 'focus_ids': [], 'people': {}}
    shift = {'id': 'projection', 'start': '2026-10-04T08:00:00+00:00',
             'end': '2026-10-04T12:00:00+00:00'}
    fast, exact = GroupResolver(data, []), GroupResolver(data, [])
    exact.day_fast_path = False
    fast_result = fast.primary('missing-person', shift)
    exact_result = exact.primary('missing-person', shift)
    assert fast_result is None and fast_result == exact_result


@pytest.mark.parametrize('people', [{}, {'known-person': {'id': 'known-person'}}])
def test_group_resolver_no_groups_needs_no_organisation(people):
    data = {'shifts': [], 'focus_ids': [], 'people': people}
    shift = {'id': 'projection', 'start': '2026-10-04T08:00:00+00:00',
             'end': '2026-10-04T12:00:00+00:00'}
    fast, exact = GroupResolver(data, []), GroupResolver(data, [])
    exact.day_fast_path = False
    assert fast.primary('known-person', shift) == exact.primary('known-person', shift) is None
    GroupResolver({'shifts': [], 'focus_ids': []}, [])
    GroupResolver({'organisation': {'name': 'Configured'}, 'shifts': [], 'focus_ids': []}, [])


@pytest.mark.parametrize('field', ['service_ids', 'capability_ids'])
def test_group_resolver_shift_dependent_membership_keeps_fast_exact_parity(field):
    data, groups = _group_data()
    data.update(types={'type': {'department_id': 'department'}},
                capabilities={'skill': {'active': True, 'starts_on': '2026-01-01'}},
                grants=[{'person_id': 'p0', 'department_id': 'department', 'service_id': 'clinic-a',
                         'capability_id': 'skill', 'starts_on': '2026-01-01'}])
    groups[0]['definition']['membership'] = {'conditions': [
        {'field': field, 'values': ['clinic-a' if field == 'service_ids' else 'skill']}]}
    fast, exact = GroupResolver(data, groups), GroupResolver(data, groups)
    exact.day_fast_path = False
    for n, service in enumerate(('clinic-a', 'clinic-b', 'clinic-a')):
        shift = {**data['shifts'][n], 'assignment_type_id': 'type', 'service_ids': [service]}
        actual = fast.primary('p0', shift)
        assert actual == exact.primary('p0', shift)
        assert (actual is not None) == (service == 'clinic-a')


@pytest.mark.parametrize('zone', ['UTC', 'America/New_York', 'Europe/Berlin'])
@pytest.mark.parametrize('day', ['2026-03-08', '2026-10-25', '2026-11-01'])
def test_group_resolver_dated_membership_and_participant_intervals_keep_parity(zone, day):
    from datetime import date, timedelta
    from packages.domain.time import local_midnight
    data, groups = _group_data()
    data['organisation']['timezone'] = zone
    data['shifts'] = []
    groups[0]['definition'].update(starts_on='2026-01-01', membership={
        'conditions': [{'field': 'category_ids', 'values': ['rank']}]})
    data.update(categories={'rank': {'active': True, 'starts_on': '2026-01-01'}},
                memberships=[{'person_id': 'p0', 'category_id': 'rank',
                              'starts_on': day, 'ends_on': str(date.fromisoformat(day) + timedelta(days=1))}])
    start = local_midnight(date.fromisoformat(day), zone)
    end = local_midnight(date.fromisoformat(day) + timedelta(days=1), zone)
    shift = {'id': 'overnight', 'start': start - timedelta(hours=2), 'end': end + timedelta(hours=2)}
    fast, exact = GroupResolver(data, groups), GroupResolver(data, groups)
    exact.day_fast_path = False
    for participant, expected in (({'start': start, 'end': end}, True), (None, False),
                                   ({'start': start + timedelta(hours=1), 'end': end - timedelta(hours=1)}, True),
                                   ({'start': end, 'end': end}, False)):
        actual = fast.primary('p0', shift, participant)
        assert actual == exact.primary('p0', shift, participant)
        assert (actual is not None) == expected
