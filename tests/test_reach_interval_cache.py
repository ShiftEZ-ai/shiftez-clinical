"""Detailed reach shares canonical clocks within one immutable snapshot read."""
import copy

import pytest

from packages.domain import validation
from packages.domain.rule_map import reach
from packages.domain.time import aware
from .test_rule_catalogue_api import line as source_line
from .test_rule_conditions import line, tree
from .test_rule_map import U, base, rule


def duties(data, rows):
    sample = data['shifts'][0]
    participant = sample['participants'][0]
    data['shifts'] = [{**copy.deepcopy(sample), 'id': U(name), 'start': start, 'end': end,
                       'participants': [{**participant, 'id': U(name + pid), 'person_id': pid,
                                         'start': start, 'end': end, 'overtime': False} for pid in people]}
                      for name, start, end, people in rows]
    data['focus_ids'] = [shift['id'] for shift in data['shifts']]
    data['availability'] = []


def catalogue(data, people, **clocks):
    source = source_line(id=U('reach-clock-source'), who=tree(line('person_ids', *people)),
                         numbers={'max': {'value': 6}}, **clocks)
    policy = rule(data, 'reach-clock-policy', {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': [source]})
    data['policies'] = [policy]
    return policy


def counted_people(found, policy):
    [source] = found['details'][policy['id']]['lines']
    return {person['person_id']: person for person in source['people']}


def test_reach_reuses_clock_builds_without_merging_people_or_duties(h, monkeypatch):
    _, data = base(h)
    first, second = h.seed['people'][:2]
    start, end = '2026-10-07T08:00:00+03:00', '2026-10-07T12:00:00+03:00'
    duties(data, [('shared-duty', start, end, [first, second]), ('distinct-duty', start, end, [first])])
    policy = catalogue(data, [first, second], weekdays=[2], start_time='08:00', end_time='12:00')
    a, b = aware(start), aware(end)
    requests, builds = [], []
    select, build = validation.scope_intervals, validation._scope_intervals

    def requested(data, scope, person_id, a, b):
        if (a, b) == (aware(start), aware(end)) and scope.get('start_time') == '08:00':
            requests.append(person_id)
        return select(data, scope, person_id, a, b)

    def built(data, scope, person_id, a, b):
        if (a, b) == (aware(start), aware(end)) and scope.get('start_time') == '08:00':
            builds.append(person_id)
        return build(data, scope, person_id, a, b)

    monkeypatch.setattr(validation, 'scope_intervals', requested)
    monkeypatch.setattr(validation, '_scope_intervals', built)
    original = copy.deepcopy(data)
    people = counted_people(reach(data, [policy], a, b, details=True), policy)
    assert set(people) == {first, second}
    assert {pid: [count['actual'] for count in row['counts']] for pid, row in people.items()} == {first: [2], second: [1]}
    assert all(row['matched_paths'] == ['who.0'] for row in people.values())
    assert {first, second} <= set(requests) and len(requests) >= 3
    assert len(builds) == 1
    assert data == original and '_scope_interval_cache' not in data


def test_reach_cache_is_fresh_after_membership_edits_and_does_not_use_callers_cache(h):
    _, data = base(h)
    first, second = h.seed['people'][:2]
    start, end = '2026-10-07T08:00:00+03:00', '2026-10-07T12:00:00+03:00'
    duties(data, [('programme-duty', start, end, [first, second])])
    programme = U('reach-clock-programme')
    data['programmes'][programme] = {'id': programme, 'active': True, 'starts_on': '2020-01-01', 'ends_on': None}
    policy = rule(data, 'programme-clock', {'kind': 'max_duties', 'maximum': 6},
                  {'person_ids': [first, second], 'programme_id': programme,
                   'weekdays': [2], 'start_time': '08:00', 'end_time': '12:00'})
    data['policies'] = [policy]
    caller_cache = {'caller-owned-sentinel': ('unchanged',)}
    data['_scope_interval_cache'] = caller_cache
    for member in (first, second, None):
        data['programme_memberships'] = ([{'id': U('reach-membership'), 'person_id': member,
            'programme_id': programme, 'starts_on': '2020-01-01', 'ends_on': '2027-01-01'}] if member else [])
        original = copy.deepcopy(data)
        found = reach(data, [policy], aware(start), aware(end), details=True)
        people = counted_people(found, policy)
        assert set(people) == ({member} if member else set())
        assert found['reach'] == {policy['id']: int(member is not None)}
        assert all([count['actual'] for count in row['counts']] == [1] for row in people.values())
        assert data == original and data['_scope_interval_cache'] is caller_cache
        assert caller_cache == {'caller-owned-sentinel': ('unchanged',)}
        assert '_scope_interval_cache' not in str(found)


def test_reach_clock_sharing_keeps_unclassified_external_work_unknown(h):
    _, data = base(h)
    person = h.seed['people'][0]
    start, end = '2026-10-07T08:00:00+03:00', '2026-10-07T12:00:00+03:00'
    duties(data, [])
    data['availability'] = [{'id': U('unclassified-external-duty'), 'person_id': person,
                             'kind': 'external_commitment', 'start': start, 'end': end}]
    policy = catalogue(data, [person], start_time='08:00', end_time='12:00')
    original = copy.deepcopy(data)
    reached = counted_people(reach(data, [policy], aware(start), aware(end), details=True), policy)
    assert set(reached) == {person}
    [count] = reached[person]['counts']
    assert count['actual'] is None and count['status'] == 'unresolved'
    assert data == original and '_scope_interval_cache' not in data


@pytest.mark.parametrize('zone,days,query,clocks,effective,rows,expected', [
    ('Asia/Riyadh', ('2026-10-14', '2026-10-16'),
     ('2026-10-14T22:00:00+03:00', '2026-10-15T03:00:00+03:00'), ('22:00', '02:00'), '2026-10-15', [
         ('before', '2026-10-14T22:00:00+03:00', '2026-10-14T23:00:00+03:00', 0),
         ('crossing', '2026-10-14T23:00:00+03:00', '2026-10-15T01:00:00+03:00', 0),
         ('inside', '2026-10-15T01:00:00+03:00', '2026-10-15T02:00:00+03:00', 1),
         ('after', '2026-10-15T02:00:00+03:00', '2026-10-15T03:00:00+03:00', 1)], [1, 1]),
    ('America/New_York', ('2026-10-31', '2026-11-03'),
     ('2026-11-01T00:00:00-04:00', '2026-11-01T03:00:00-05:00'), ('00:00', '03:00'), '2026-11-01', [
         ('first-fold', '2026-11-01T01:15:00-04:00', '2026-11-01T01:45:00-04:00', 0),
         ('second-fold', '2026-11-01T01:15:00-05:00', '2026-11-01T01:45:00-05:00', 0),
         ('other-person', '2026-11-01T01:15:00-05:00', '2026-11-01T01:45:00-05:00', 1)], [2, 1]),
], ids=['overnight-effective-date', 'distinct-fall-back-instants'])
def test_reach_cached_clocks_keep_effective_midnight_and_distinct_dst_instants(h, zone, days, query, clocks, effective, rows, expected):
    _, data = base(h)
    people = h.seed['people'][:2]
    data['organisation']['timezone'] = zone
    data['workspace'].update(starts_on=days[0], ends_on=days[1])
    duties(data, [(name, start, end, [people[index]]) for name, start, end, index in rows])
    policy = catalogue(data, people, start_time=clocks[0], end_time=clocks[1])
    policy['starts_on'] = effective
    original = copy.deepcopy(data)
    reached = counted_people(reach(data, [policy], *map(aware, query), details=True), policy)
    assert set(reached) == set(people)
    assert {pid: [count['actual'] for count in reached[pid]['counts']] for pid in people} == {
        pid: [count] for pid, count in zip(people, expected)}
    assert all(row['matched_paths'] == ['who.0'] for row in reached.values())
    assert data == original and '_scope_interval_cache' not in data
