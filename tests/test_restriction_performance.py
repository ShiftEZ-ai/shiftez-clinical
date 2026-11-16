"""Restriction preparation stays linear in dated records, with shared-CI timing headroom."""
import copy
import time
from datetime import timedelta

import pytest

from packages.contracts.schemas import PolicyDefinition
from packages.domain.candidate_evidence import candidate_evidence
from packages.domain.conditions import change_points
from packages.domain.time import aware
from packages.domain.validation import applicable_restriction_intervals, index_person_rows, restriction_blocks
from tests.test_performance_budget import python_budget


class CountRows(list):
    visits = 0

    def __iter__(self):
        for row in super().__iter__():
            self.visits += 1
            yield row


def restriction_data(people, field='tags', rules=1):
    definition = PolicyDefinition.model_validate({'rule': {'kind': 'restriction'}, 'scope': {'conditions': {
        'match': 'all', 'conditions': [{'field': field, 'op': 'any', 'values': ['restricted']}]}}}).model_dump(mode='json')
    dates = {'starts_on': '2020-01-01', 'ends_on': None}
    data = {'organisation': {'timezone': 'UTC'}, 'people': {str(i): {'active': True, 'tags': []} for i in range(people)},
            'types': {'type': {'department_id': 'department'}}, 'exceptions': [], '_scope_interval_cache': {},
            '_person_dates_cache': {}}
    for name, key, value in (('memberships', 'category_id', 'category'), ('grants', 'capability_id', 'capability'),
                             ('programme_memberships', 'programme_id', 'programme'), ('rotations', 'rotation_type_id', 'rotation'),
                             ('eligibilities', 'service_id', 'clinic')):
        data[name] = CountRows({'person_id': str(i), 'department_id': 'department', key: value, **dates}
                               for i in range(people))
    for name, key in (('categories', 'category'), ('capabilities', 'capability'), ('programmes', 'programme'),
                      ('rotation_types', 'rotation'), ('cohorts', 'cohort')):
        data[name] = {key: {'active': True, **dates}}
    data['policies'] = [{'id': f'restriction-{i}', 'revision': 1, 'active': True, 'status': 'active',
                         'department_id': 'department', 'definition': copy.deepcopy(definition), **dates} for i in range(rules)]
    data['_person_rows'] = index_person_rows(data)
    return data


def duty(n):
    a = aware('2026-10-04T08:00:00+00:00') + timedelta(days=n // 17, minutes=(n % 17) * 10)
    b = a + timedelta(hours=4)
    return {'id': str(n), 'assignment_type_id': 'type', 'service_ids': [], 'location_id': None,
            'start': a.isoformat(), 'end': b.isoformat(), 'type_snapshot': {'definition': {}}}, a, b


@pytest.mark.parametrize('people', (4, 8, 16))
@pytest.mark.parametrize('field', ('tags', 'category_ids'))
def test_restriction_scans_stay_linear_in_department_rows(people, field):
    data = restriction_data(people, field, rules=3)
    tables = [data[name] for name in ('memberships', 'grants', 'programme_memberships', 'rotations', 'eligibilities')]
    for rows in tables:
        rows.visits = 0
    for n in (0, 17):
        shift, a, b = duty(n)
        for pid in data['people']:
            assert not restriction_blocks(data, pid, shift, a, b)
    visits = sum(rows.visits for rows in tables)
    print('restriction row visits', {'people': people, 'field': field, 'rows': sum(map(len, tables)), 'visits': visits})
    assert visits <= sum(map(len, tables))
    if field == 'tags':
        assert not data['_person_dates_cache']


def test_change_points_visit_each_indexed_row_once_across_sessions_and_rules():
    data = restriction_data(16)
    tables = [data[name] for name in ('memberships', 'grants', 'programme_memberships', 'rotations', 'eligibilities')]
    indexed = []
    for name in ('memberships', 'grants', 'programme_memberships', 'rotations', 'eligibilities'):
        for pid, rows in data['_person_rows'][name].items():
            rows = data['_person_rows'][name][pid] = CountRows(rows)
            indexed.append(rows)
    for n in range(50):
        _, a, b = duty(n * 17)
        for pid in data['people']:
            for _ in range(3):
                assert change_points(data, pid, a, b) == [a]
    visits = sum(rows.visits for rows in [*tables, *indexed])
    assert visits <= 2 * sum(map(len, indexed)), visits


@pytest.mark.parametrize('field', ('tags', 'person_ids', 'service_ids', 'location_ids', 'assignment_type_ids'))
@pytest.mark.parametrize('match', ('all', 'any'))
def test_static_nested_scopes_skip_person_dates(monkeypatch, field, match):
    from packages.domain import validation
    data = restriction_data(4, field)
    scope = data['policies'][0]['definition']['scope']
    scope['conditions']['conditions'][0]['op'] = 'none'
    scope['conditions'] = {'match': match, 'conditions': [scope['conditions']]}
    def unexpected(*args):
        pytest.fail('Static restriction read dated person records')
    monkeypatch.setattr(validation, 'change_points', unexpected)
    shift, a, b = duty(0)
    assert restriction_blocks(data, '0', shift, a, b)


@pytest.mark.parametrize('date_field', ('starts_on', 'ends_on'))
def test_static_scope_keeps_policy_effective_date_cuts(date_field):
    data = restriction_data(1)
    data['people']['0']['tags'] = ['restricted']
    policy = data['policies'][0]
    policy[date_field] = '2026-10-05'
    a, midnight, b = map(aware, ('2026-10-04T22:00:00+00:00', '2026-10-05T00:00:00+00:00', '2026-10-05T02:00:00+00:00'))
    shift, _, _ = duty(0)
    expected = (midnight, b) if date_field == 'starts_on' else (a, midnight)
    assert list(applicable_restriction_intervals(data, policy, '0', shift, a, b)) == [expected]
    assert not data['_person_dates_cache']


def test_selector_starting_inside_overnight_duty_restricts_the_second_half():
    from packages.domain.conditions import OUTRANKED
    data = restriction_data(1)
    policy = data['policies'][0]
    policy['definition']['scope']['conditions'] = {'match': 'all', 'conditions': [
        {'field': OUTRANKED, 'op': 'any', 'values': ['midnight-selector']}]}
    policy['_catalogue_sources'] = {'midnight-selector': {
        'starts_on': '2026-10-05', 'ends_on': None, 'definition': {'scope': {}}}}
    a, midnight, b = map(aware, ('2026-10-04T22:00:00+00:00', '2026-10-05T00:00:00+00:00', '2026-10-05T02:00:00+00:00'))
    shift, _, _ = duty(0)
    shift.update(start=a.isoformat(), end=b.isoformat())
    assert policy['definition']['severity'] == 'mandatory'
    assert list(applicable_restriction_intervals(data, policy, '0', shift, a, b)) == [(midnight, b)]
    assert restriction_blocks(data, '0', shift, a, b)
    assert restriction_blocks(data, '0', shift, midnight, b)
    assert not restriction_blocks(data, '0', shift, a, midnight)


@pytest.mark.parametrize('date_field', ('starts_on', 'ends_on'))
def test_static_scope_miss_checks_internal_policy_date_before_skipping_clocks(monkeypatch, date_field):
    from packages.domain import validation
    data = restriction_data(1)
    policy = data['policies'][0]
    policy[date_field] = '2026-10-05'
    a, midnight, b = map(aware, ('2026-10-04T22:00:00+00:00', '2026-10-05T00:00:00+00:00', '2026-10-05T02:00:00+00:00'))
    shift, _, _ = duty(0)
    intervals = []
    original = validation.restricted_intervals
    def checked(*args):
        intervals.append(original(*args))
        return intervals[-1]
    monkeypatch.setattr(validation, 'restricted_intervals', checked)
    assert not restriction_blocks(data, '0', shift, a, b)
    expected = [(midnight, b) if date_field == 'starts_on' else (a, midnight)]
    assert intervals and all(result == expected for result in intervals)
    assert not data['_person_dates_cache']


def test_static_scope_miss_skips_clocks_without_caching_the_person(monkeypatch):
    from packages.domain import validation
    data = restriction_data(1, 'tags')
    shift, a, b = duty(0)
    original = validation.restricted_intervals
    def unexpected(*args):
        pytest.fail('A static scope miss read restriction clocks')
    monkeypatch.setattr(validation, 'restricted_intervals', unexpected)
    assert not restriction_blocks(data, '0', shift, a, b)
    monkeypatch.setattr(validation, 'restricted_intervals', original)
    data['people']['0']['tags'] = ['restricted']
    assert restriction_blocks(data, '0', shift, a, b)


def test_cached_points_keep_interval_boundaries_and_do_not_survive_a_new_check(h):
    from packages.domain.validation import evaluate
    from tests.test_engine_restriction_boundaries import dated_scope, policy, selected_scope, snapshot, POLICY
    data = snapshot(h)
    pid = h.seed['people'][0]
    dated_scope(data, pid, 'programme_ids', 'join')
    data['policies'] = [policy(data, {'kind': 'restriction'}, selected_scope('programme_ids', True))]
    assert any(f['rule_id'] == POLICY for f in evaluate(data)['findings'])
    assert '_person_dates_cache' not in data
    # A later check sees an edited effective date rather than the previous check's boundaries.
    data['programme_memberships'][0]['starts_on'] = '2026-10-06'
    assert not any(f['rule_id'] == POLICY for f in evaluate(data)['findings'])
    data['_person_rows'], data['_person_dates_cache'] = index_person_rows(data), {}
    a, b, c = map(aware, ('2026-10-05T00:00:00+03:00', '2026-10-06T00:00:00+03:00', '2026-10-07T00:00:00+03:00'))
    assert change_points(data, pid, a, c) == [a, b]
    assert change_points(data, pid, b, c) == [b]
    points = change_points(data, pid, a, b)
    points.clear()
    assert change_points(data, pid, a, b) == [a]


@pytest.mark.parametrize('engine', ('cp_sat', 'greedy'))
def test_generation_shares_indexed_change_points_with_candidate_filtering(h, monkeypatch, engine):
    from packages.domain import conditions, cp_sat, generation
    from tests.test_engine_restriction_boundaries import dated_scope, policy, selected_scope, snapshot
    data = snapshot(h)
    pid = h.seed['people'][0]
    dated_scope(data, pid, 'programme_ids', 'join')
    data['policies'] = [policy(data, {'kind': 'restriction'}, selected_scope('programme_ids', True))]
    data['shifts'][0]['participants'] = []
    seen = {}
    original = conditions._change_points
    def checked(view, person):
        assert '_person_rows' in view and '_person_dates_cache' in view
        cache = view['_person_dates_cache']
        key = (id(cache), person)
        # Hold each cache alive so a fresh final validation cannot recycle its id.
        count, _ = seen.get(key, (0, cache))
        seen[key] = (count + 1, cache)
        return original(view, person)
    monkeypatch.setattr(conditions, '_change_points', checked)
    result = (cp_sat.generate(data, 3, workers=1, feasibility_only=True) if engine == 'cp_sat'
              else generation.generate(data, 3))
    assert result['status'] == 'candidate_feasible', result['status']
    assert seen and all(count == 1 for count, _ in seen.values())
    assert not any(row['participant']['person_id'] == pid for row in result['additions'])
    assert '_person_dates_cache' not in data


@pytest.fixture(scope="module", params=('tags', 'category_ids'))
def broad_restriction_preparation(request):
    field = request.param
    with pytest.MonkeyPatch.context() as monkeypatch:
        from packages.domain import validation
        data = restriction_data(150, field)
        stamps, eligible = 0, []
        original = validation.local_stamp
        def counted(*args):
            nonlocal stamps
            stamps += 1
            return original(*args)
        monkeypatch.setattr(validation, 'local_stamp', counted)
        started = time.process_time()  # CPU time: other jobs on a shared runner must not count
        for n in range(500):
            shift, _, _ = duty(n)
            for pid in data['people']:
                eligible.append(candidate_evidence(data, pid, shift)['eligible_and_available'])
        elapsed = time.process_time() - started
        print('restriction department preparation', {'people': 150, 'sessions': 500, 'field': field,
                                                     'seconds': elapsed, 'clock_stamps': stamps})
        return eligible, stamps, elapsed


def test_broad_restriction_candidate_preparation_at_department_scale(broad_restriction_preparation):
    eligible, stamps, _ = broad_restriction_preparation
    assert len(eligible) == 500 * 150 and all(eligible)
    # Previously 302,000 clock stamps: one rule and one scope window per duty need at most 4,000.
    assert stamps <= 8 * 500, stamps


@pytest.mark.cpu_budget
def test_broad_restriction_candidate_preparation_at_department_scale_cpu_budget(broad_restriction_preparation):
    _, _, elapsed = broad_restriction_preparation
    # Cached: 3.0 s native / 8.5 s traced; CI was 41 s vs 11.6 s locally (~3.5x), projecting ~30 s after.
    # Ceilings of 6 s native / 42 s traced leave CI headroom; the clock-stamp count guards repeated work.
    assert elapsed < python_budget(6), elapsed


def test_restriction_clock_cache_keeps_effective_dates_and_returns_fresh_intervals():
    from packages.domain.validation import restricted_intervals
    data = restriction_data(2)
    policy = data['policies'][0]
    a, b = map(aware, ('2026-10-04T22:00:00+00:00', '2026-10-05T02:00:00+00:00'))
    uncached = {k: v for k, v in data.items() if k != '_scope_interval_cache'}
    for starts_on, ends_on in ((None, None), ('2026-10-05', None), (None, '2026-10-05')):
        policy.update(starts_on=starts_on, ends_on=ends_on)
        for weekdays, start_time, end_time in ((list(range(7)), '00:00', '24:00'), ([6], '23:00', '01:00')):
            policy['definition']['rule'].update(weekdays=weekdays, start_time=start_time, end_time=end_time)
            expected = restricted_intervals(uncached, policy, a, b, '0')
            result = restricted_intervals(data, policy, a, b, '0')
            assert result == expected
            result.clear()
            assert restricted_intervals(data, policy, a, b, '1') == expected
    data['organisation']['timezone'] = 'Asia/Riyadh'
    assert restricted_intervals(data, policy, a, b, '0') == restricted_intervals(uncached, policy, a, b, '0')


def test_restriction_clock_cache_never_reuses_another_persons_leave_buffer():
    from packages.domain.validation import restricted_intervals
    data = restriction_data(2)
    policy = data['policies'][0]
    policy['definition']['rule']['leave_buffer_days'] = 1
    data['availability'] = [{'person_id': '0', 'kind': 'approved_leave',
                             'start': '2026-10-05T00:00:00+00:00', 'end': '2026-10-06T00:00:00+00:00'}]
    data['_person_rows'] = index_person_rows(data)
    _, a, b = duty(0)
    assert restricted_intervals(data, policy, a, b, '0') == [(a, b)]
    assert restricted_intervals(data, policy, a, b, '1') == []
    data['availability'][0]['person_id'] = '1'
    data['_person_rows'] = index_person_rows(data)
    assert restricted_intervals(data, policy, a, b, '0') == []
    assert restricted_intervals(data, policy, a, b, '1') == [(a, b)]
