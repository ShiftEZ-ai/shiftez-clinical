"""Resolver-owned clocks never retain candidate membership or evaluation answers."""
import copy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from apps.api.services import issues, staffing
from packages.domain import validation
from packages.domain.time import aware


def view(cache, zone='Asia/Riyadh', **rows):
    return {'organisation': {'timezone': zone}, '_resolver_clock_cache': cache, **rows}


def intervals(rows):
    return [(aware(a), aware(b)) for a, b in rows]


def test_shared_clocks_keep_candidate_programme_and_rotation_intersections_fresh(monkeypatch):
    cache, builds = {}, []
    real = validation.restricted_intervals
    monkeypatch.setattr(validation, 'restricted_intervals', lambda *args, **kwargs: builds.append(1) or real(*args, **kwargs))
    a, b = map(aware, ('2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'))
    scope = {'start_time': '08:00', 'end_time': '12:00', 'programme_id': 'programme', 'rotation_type_id': 'rotation'}
    static = {'programmes': {'programme': {'active': True}}, 'rotation_types': {'rotation': {'active': True}},
              'cohorts': {'cohort': {'active': True, 'programme_id': 'programme'}},
              'programme_memberships': [{'person_id': 'p', 'programme_id': 'programme', 'starts_on': '2026-10-04', 'ends_on': '2026-10-05'}],
              'rotations': [{'person_id': 'p', 'rotation_type_id': 'rotation', 'cohort_id': 'cohort', 'starts_on': '2026-10-04', 'ends_on': '2026-10-05'}]}
    first = view(cache, **copy.deepcopy(static))
    assert validation.scope_intervals(first, scope, 'p', a, b) == [(a, b)]
    assert validation.scope_intervals(first, scope, 'q', a, b) == []
    for change in ('programme_memberships', 'rotations', 'parent'):
        candidate = view(cache, **copy.deepcopy(static))
        if change == 'parent':
            candidate['programmes']['programme']['active'] = False
        else:
            candidate[change] = []
        assert validation.scope_intervals(candidate, scope, 'p', a, b) == []
    assert len(builds) == 1
    assert {key[0] for key in cache} == {'clocks', 'restriction'}


def test_clock_cache_separates_timezone_clocks_and_exact_query_bounds():
    cache = {}
    a, b = map(aware, ('2026-10-04T00:00:00Z', '2026-10-05T00:00:00Z'))
    scope = {'start_time': '08:00', 'end_time': '12:00'}
    assert validation.scope_intervals(view(cache), scope, 'p', a, b) == [tuple(map(aware, ('2026-10-04T05:00:00Z', '2026-10-04T09:00:00Z')))]
    assert validation.scope_intervals(view(cache, 'UTC'), scope, 'p', a, b) == [tuple(map(aware, ('2026-10-04T08:00:00Z', '2026-10-04T12:00:00Z')))]
    assert validation.scope_intervals(view(cache), {**scope, 'start_time': '09:00'}, 'p', a, b) == [tuple(map(aware, ('2026-10-04T06:00:00Z', '2026-10-04T09:00:00Z')))]
    assert validation.scope_intervals(view(cache), {**scope, 'weekdays': [0]}, 'p', a, b) == []
    lo, hi = map(aware, ('2026-10-04T06:00:00Z', '2026-10-04T07:00:00Z'))
    assert validation.scope_intervals(view(cache), scope, 'p', lo, hi) == [(lo, hi)]


def test_cached_windows_return_fresh_lists_and_respect_effective_midnight():
    cache = {}
    data = view(cache)
    a, b = map(aware, ('2026-10-14T22:00:00+03:00', '2026-10-15T03:00:00+03:00'))
    rule = {'weekdays': list(range(7)), 'start_time': '22:00', 'end_time': '02:00'}
    policy = {'definition': {'rule': rule}, 'starts_on': '2026-10-15'}
    expected = [tuple(map(aware, ('2026-10-15T00:00:00+03:00', '2026-10-15T02:00:00+03:00')))]
    found = validation.restricted_intervals(data, policy, a, b)
    assert found == expected
    found.clear()
    assert validation.restricted_intervals(data, policy, a, b) == expected
    assert validation.restricted_intervals(data, {**policy, 'starts_on': '2026-10-16'}, a, b) == []
    assert validation.restricted_intervals(data, {**policy, 'ends_on': '2026-10-15'}, a, b) == []


def test_shared_clock_key_distinguishes_both_dst_fold_instants():
    cache = {}
    zone = ZoneInfo('America/New_York')
    scope = {'start_time': '00:00', 'end_time': '03:00'}
    observed = []
    for fold in (0, 1):
        a, b = (datetime(2026, 11, 1, 1, minute, tzinfo=zone, fold=fold) for minute in (15, 45))
        found = validation.scope_intervals(view(cache, zone.key), scope, 'p', a, b)
        assert intervals(found) == [(aware(a), aware(b))]
        observed.append(intervals(found))
    assert observed[0] != observed[1]
    assert len([key for key in cache if key[0] == 'clocks']) == 2


def test_leave_buffers_do_not_share_candidate_absence_answers():
    cache = {}
    a, b = map(aware, ('2026-10-04T00:00:00Z', '2026-10-05T00:00:00Z'))
    policy = {'definition': {'rule': {'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00', 'leave_buffer_days': 1}}}
    data = view(cache, 'UTC', availability=[{'person_id': 'p', 'kind': 'recorded_leave', 'start': a.isoformat(), 'end': b.isoformat()}])
    assert validation.restricted_intervals(data, policy, a, b, 'q') == []
    assert validation.restricted_intervals(data, policy, a, b, 'p') == [(a, b)]
    data['availability'] = []
    assert validation.restricted_intervals(data, policy, a, b, 'p') == []
    assert cache == {}


@pytest.mark.parametrize('admitted', [65535, 65536])
def test_clock_admission_ceiling_falls_back_without_truncating(admitted):
    cache = {('sentinel', n): () for n in range(admitted)}
    a, b = map(aware, ('2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'))
    scope = {'start_time': '08:00', 'end_time': '12:00'}
    assert validation.scope_intervals(view(cache), scope, 'p', a, b) == [(a, b)]
    assert validation.scope_intervals(view(cache), scope, 'q', a, b) == [(a, b)]
    assert len(cache) == 65536


def resolver_input(h):
    from .test_resolve_issues import leave_rota
    w = leave_rota(h, days=('04',))
    data = h.snapshot(w)
    alex, jordan, taylor = h.seed['people']
    # Two simultaneous places: the first replacement must become a live commitment for the next.
    shift = next(s for s in data['shifts'] if s['id'] in data['focus_ids'])
    second = copy.deepcopy(shift)
    second['id'] = 'second-session'
    second['participants'][0].update(id='second-seat', shift_id=second['id'])
    data['shifts'].append(second)
    data['focus_ids'].append(second['id'])
    targets = {(s['id'], alex): ['issue-' + s['id']] for s in (shift, second)}
    return data, targets, {jordan, taylor}


@pytest.mark.parametrize('resolver', [False, True])
def test_assignment_preparation_respects_resolver_cache_ownership(h, resolver):
    data, _, _ = resolver_input(h)
    intersections = {('caller-membership',): ()}
    data['_scope_interval_cache'] = intersections
    clocks = {('caller-clock',): ()}
    if resolver:
        data['_resolver_clock_cache'] = clocks
    original = copy.deepcopy(data)
    prepared = validation.prepare_assignment_trials(data)
    for snapshot in (prepared, validation.prepare_assignment_trials(prepared)):
        if resolver:
            assert '_scope_interval_cache' not in snapshot
            assert snapshot['_resolver_clock_cache'] is clocks
        else:
            assert snapshot['_scope_interval_cache'] is intersections
    assert data == original and data['_scope_interval_cache'] is intersections


def test_resolver_parity_sequential_commitments_cache_isolation_and_freshness(h, monkeypatch):
    data, targets, candidates = resolver_input(h)
    caller_clocks, caller_intersections = {('caller',): ()}, {('caller-membership',): ()}
    data.update(_resolver_clock_cache=caller_clocks, _scope_interval_cache=caller_intersections)
    original = copy.deepcopy(data)
    seen = []
    real_evaluate = staffing.evaluate

    def observed(snapshot, **kwargs):
        seen.append(snapshot)
        assert '_scope_interval_cache' not in snapshot
        return real_evaluate(snapshot, **kwargs)

    monkeypatch.setattr(staffing, 'evaluate', observed)
    result = issues.best_replacements(data, targets, candidates)
    assert len(result) == 2 and {row['replacement_id'] for row in result} == candidates
    assert all(row['status'] == 'ok' for row in result)
    first_cache = seen[0]['_resolver_clock_cache']
    assert first_cache is not caller_clocks and all(s['_resolver_clock_cache'] is first_cache for s in seen)
    assert data == original and data['_resolver_clock_cache'] is caller_clocks and data['_scope_interval_cache'] is caller_intersections

    def uncached(snapshot, **kwargs):
        # Same canonical evaluator and resolver; only the new preparation opt-in is removed.
        bare = dict(snapshot)
        bare.pop('_resolver_clock_cache', None)
        return real_evaluate(bare, **kwargs)

    monkeypatch.setattr(staffing, 'evaluate', uncached)
    # Cheap checks also use the canonical helpers with the cache disabled.
    real_index = issues.people_index
    def uncached_index(source):
        index = real_index(source)
        index['fast'].pop('_resolver_clock_cache', None)
        return index
    monkeypatch.setattr(issues, 'people_index', uncached_index)
    assert issues.best_replacements(data, targets, candidates) == result

    monkeypatch.setattr(issues, 'people_index', real_index)
    monkeypatch.setattr(staffing, 'evaluate', observed)
    seen.clear()
    again = issues.best_replacements(data, targets, candidates)
    assert again == result and seen[0]['_resolver_clock_cache'] is not first_cache
    # A later call with changed dated availability must not inherit the earlier candidate result.
    changed = copy.deepcopy(data)
    person = next(iter(candidates))
    changed['availability'].append({'person_id': person, 'kind': 'recorded_leave',
                                    'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00'})
    later = issues.best_replacements(changed, targets, candidates)
    assert sum(row['replacement_id'] is not None for row in later) == 1


def test_resolver_clock_lifetime_ends_on_exception(h, monkeypatch):
    data, targets, candidates = resolver_input(h)
    captured = []
    sentinel = RuntimeError('stop this resolver')
    def fail(snapshot, **kwargs):
        captured.append(snapshot['_resolver_clock_cache'])
        raise sentinel
    monkeypatch.setattr(staffing, 'evaluate', fail)
    for _ in range(2):
        with pytest.raises(RuntimeError) as error:
            issues.best_replacements(data, targets, candidates)
        assert error.value is sentinel
    assert captured[0] is not captured[1] and '_resolver_clock_cache' not in data
