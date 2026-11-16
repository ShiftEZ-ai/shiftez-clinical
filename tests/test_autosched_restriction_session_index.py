"""Session restriction selection retains complete rule context and canonical candidate evidence."""
from collections import Counter
import copy
import hashlib
import json
import time

import pytest

from packages.contracts.schemas import GenerationOptions
from packages.domain import cp_sat, validation
import packages.domain.candidate_evidence as evidence_module
from packages.domain.conditions import OUTRANKED
from packages.domain.solver_control import SolverControl
from packages.domain.time import aware
from tests.test_candidate_restriction_index import CountedPolicies
from tests.test_engine_restriction_boundaries import SELECTED, dated_scope, selected_scope
from tests.test_performance_budget import python_budget
from tests.test_restriction_performance import duty, restriction_data
from tests.test_schedule_score_scale import department


def source(people=3, rules=1):
    data = restriction_data(people, rules=rules)
    data['availability'] = []
    for grant in data['grants']:
        grant['service_id'] = None
    shift, _, _ = duty(0)
    shift.update(start='2026-10-04T22:00:00+00:00', end='2026-10-05T02:00:00+00:00')
    data['shifts'] = [shift]
    for policy in data['policies']:
        policy['name'] = 'Synthetic restriction'
        policy['definition']['scope'] = {}
    return data, data['policies'][0], shift, aware(shift['start']), aware(shift['end'])


def uncached(data):
    return {**data, '_line_index_cache': None, '_restriction_index_cache': None}


@pytest.mark.parametrize('cache_key', ['_line_index_cache', '_restriction_index_cache'])
@pytest.mark.parametrize('field', [None, 'person_ids', 'category_ids', 'capability_ids', 'programme_ids', 'mixed_or'])
def test_session_index_keeps_exact_intervals_and_ordered_candidate_reasons(monkeypatch, cache_key, field):
    data, policy, shift, a, b = source()
    if field == 'person_ids':
        policy['definition']['scope'] = {'person_id': '0'}
    elif field == 'mixed_or':
        dated_scope(data, '0', 'programme_ids', 'join')
        policy['definition']['scope'] = {'conditions': {'match': 'any', 'conditions': [
            {'field': 'programme_ids', 'op': 'any', 'values': [SELECTED]},
            {'field': 'service_ids', 'op': 'any', 'values': ['different-clinic']}]}}
    elif field:
        dated_scope(data, '0', field, 'join')
        policy['definition']['scope'] = selected_scope(field, True)
    data['_person_rows'] = validation.index_person_rows(data)
    irrelevant = copy.deepcopy(policy)
    irrelevant.update(id='irrelevant-clinic')
    irrelevant['definition']['scope'] = {'service_ids': ['different-clinic']}
    second = copy.deepcopy(policy)
    second['id'] = 'second-restriction'
    data['policies'] = [irrelevant, policy, second]
    expected_intervals = {(p['id'], pid): list(validation.applicable_restriction_intervals(
        uncached(data), p, pid, shift, a, b)) for p in data['policies'] for pid in data['people']}
    expected = {pid: evidence_module.candidate_evidence(uncached(data), pid, shift) for pid in data['people']}
    assert expected_intervals[policy['id'], '0']
    assert all(not expected_intervals['irrelevant-clinic', pid] for pid in data['people'])
    counted = data['policies'] = CountedPolicies(data['policies'])
    data[cache_key] = {}
    original = validation.applicable_restriction_intervals
    checks = []

    def intervals(view, row, pid, session, start, end):
        assert view['policies'] is counted
        result = list(original(view, row, pid, session, start, end))
        assert result == expected_intervals[row['id'], pid]
        checks.append(row['id'])
        yield from result

    monkeypatch.setattr(validation, 'applicable_restriction_intervals', intervals)
    for _ in range(2):
        for pid in data['people']:
            assert evidence_module.candidate_evidence(data, pid, shift) == expected[pid]
    assert checks and 'irrelevant-clinic' not in checks
    assert counted.visits == len(counted)


@pytest.mark.parametrize('zone', ['UTC', 'Europe/London'])
@pytest.mark.parametrize('boundary', ['starts_on', 'ends_on', 'selector'])
def test_session_index_preserves_source_dates_partial_clocks_overnight_and_dst(zone, boundary):
    data, policy, shift, _, _ = source()
    data['organisation']['timezone'] = zone
    a, b = map(aware, ('2026-10-24T22:00:00+00:00', '2026-10-25T03:00:00+00:00'))
    shift.update(start=a.isoformat(), end=b.isoformat())
    policy['definition']['rule'].update(start_time='23:00', end_time='02:00')
    if boundary == 'selector':
        policy['definition']['scope'] = {'conditions': {'match': 'all', 'conditions': [
            {'field': 'assignment_type_ids', 'op': 'any', 'values': ['type']},
            {'field': OUTRANKED, 'op': 'any', 'values': ['midnight-selector']}]}}
        policy['_catalogue_sources'] = {'midnight-selector': {
            'starts_on': '2026-10-25', 'ends_on': None, 'definition': {'scope': {}}}}
        other = copy.deepcopy(policy)
        other['id'] = 'non-restriction-source'
        other['definition']['rule'] = {'kind': 'max_duties', 'maximum': 2, 'window': 'day'}
        data['policies'].insert(0, other)
    else:
        policy[boundary] = '2026-10-25'
    expected = {pid: evidence_module.candidate_evidence(uncached(data), pid, shift) for pid in data['people']}
    intervals = list(validation.applicable_restriction_intervals(uncached(data), policy, '0', shift, a, b))
    assert intervals
    data['_line_index_cache'] = {}
    for pid in data['people']:
        assert evidence_module.candidate_evidence(data, pid, shift) == expected[pid]
        assert list(validation.applicable_restriction_intervals(data, policy, pid, shift, a, b)) == intervals


def test_session_views_with_the_same_id_retain_distinct_clinic_and_usual_assignee_answers():
    data, policy, shift, a, b = source()
    policy['definition']['scope'] = {'conditions': {'match': 'all', 'conditions': [
        {'field': 'usual_assignee_ids', 'op': 'any', 'values': ['0']}]}}
    shift['series_snapshot'] = {'definition': {'default_person_ids': ['0']}}
    other = {**shift, 'series_snapshot': {'definition': {'default_person_ids': ['1']}}}
    projection = {**other, '_quota_subject_context': True}
    data['_restriction_index_cache'] = {}
    for view, expected in ((shift, [policy]), (other, []), (projection, [policy]), (shift, [policy])):
        assert list(validation.blocking_restrictions(uncached(data), '0', view, a, b)) == expected
        assert list(validation.blocking_restrictions(data, '0', view, a, b)) == expected
    selected = data['_restriction_index_cache'][id(data['policies'])][6]
    assert len(selected) == 3 and all(selected[id(view), a, b][0] is view for view in (shift, other, projection))
    policy = copy.deepcopy(policy)
    policy['definition']['scope'] = {'service_ids': ['clinic']}
    data['policies'] = [policy]
    clinic = {**shift, 'service_ids': ['clinic']}
    other = {**shift, 'service_ids': ['different-clinic']}
    for view in (clinic, other, clinic):
        assert list(validation.blocking_restrictions(data, '0', view, a, b)) == list(
            validation.blocking_restrictions(uncached(data), '0', view, a, b))


def test_date_projection_and_missing_session_do_not_use_the_new_prefilter(monkeypatch):
    data, policy, shift, a, b = source()
    policy['definition']['scope'] = {'service_ids': ['different-clinic']}
    projected = {**shift, '_date_projection': True}
    expected = list(validation.blocking_restrictions(uncached(data), '0', projected, a, b))
    assert expected == [policy]
    data['_restriction_index_cache'] = {}

    def unexpected(*args):
        pytest.fail('Projected or absent sessions must not use the session predicate')

    monkeypatch.setattr(validation, 'session_matches', unexpected)
    assert list(validation.blocking_restrictions(data, '0', projected, a, b)) == expected
    monkeypatch.setattr(validation, 'applicable_restriction_intervals', lambda *args: iter([(a, b)]))
    assert list(validation.blocking_restrictions(data, '0', None, a, b)) == [policy]


def test_leave_buffers_and_exception_checks_stay_person_specific(monkeypatch):
    data, policy, shift, _, _ = source()
    policy['definition']['rule']['leave_buffer_days'] = 1
    data['availability'] = [{'id': 'leave', 'person_id': '0', 'kind': 'approved_leave',
                             'start': '2026-10-05T00:00:00+00:00', 'end': '2026-10-06T00:00:00+00:00'}]
    data['_person_rows'] = validation.index_person_rows(data)
    expected = {pid: evidence_module.candidate_evidence(uncached(data), pid, shift) for pid in data['people']}
    assert any(row['code'] == 'RESTRICTION' for row in expected['0']['exclusions'])
    assert not expected['1']['exclusions']
    data['_line_index_cache'] = {}
    for pid in data['people']:
        assert evidence_module.candidate_evidence(data, pid, shift) == expected[pid]
    calls = []

    def exception(view, row, pid, a, b):
        calls.append(pid)
        return None

    monkeypatch.setattr(validation, 'exception_for', exception)
    evidence_module.candidate_evidence(data, '0', shift)
    evidence_module.candidate_evidence(data, '1', shift)
    assert calls == ['0']


def test_new_policy_source_and_fresh_edited_facts_do_not_reuse_old_session_answers():
    data, policy, shift, a, b = source()
    policy['definition']['scope'] = {'service_ids': ['different-clinic']}
    data['_restriction_index_cache'] = {}
    original_source = data['policies']
    assert not list(validation.blocking_restrictions(data, '0', shift, a, b))
    replacement = copy.deepcopy(original_source)
    replacement[0]['definition']['scope'] = {}
    trial = {**data, 'policies': replacement}
    assert list(validation.blocking_restrictions(trial, '0', shift, a, b)) == replacement
    assert data['_restriction_index_cache'][id(original_source)][0] is original_source
    assert data['_restriction_index_cache'][id(replacement)][0] is replacement
    policy['definition']['scope'] = {}
    fresh = {**data, '_restriction_index_cache': {}}
    assert list(validation.blocking_restrictions(fresh, '0', shift, a, b)) == [policy]
    policy['starts_on'] = '2026-10-06'
    assert not list(validation.blocking_restrictions({**fresh, '_restriction_index_cache': {}}, '0', shift, a, b))


def test_session_identity_cache_is_bounded():
    data, policy, shift, a, b = source()
    data['_restriction_index_cache'] = {}
    assert list(validation.blocking_restrictions(data, '0', shift, a, b)) == [policy]
    sessions = data['_restriction_index_cache'][id(data['policies'])][6]
    sessions.update({n: None for n in range(65_535)})
    assert len(sessions) == 65_536
    for _ in range(2):
        clone = {**shift}
        assert list(validation.blocking_restrictions(data, '1', clone, a, b)) == [policy]
        assert (id(clone), a, b) not in sessions
    assert len(sessions) == 65_536


@pytest.mark.parametrize('boundary', ['starts_on', 'ends_on'])
def test_effective_date_prefilter_keeps_partial_windows_and_caches_each_interval(monkeypatch, boundary):
    data, policy, shift, a, b = source()
    midnight = aware('2026-10-05T00:00:00+00:00')
    policy[boundary] = '2026-10-05'
    other = copy.deepcopy(policy)
    other.update(id='different-department', department_id='different-department')
    data['policies'].insert(0, other)
    windows = [(a, b), (a, midnight), (midnight, b)]
    expected = [list(validation.blocking_restrictions(uncached(data), '0', shift, lo, hi))
                for lo, hi in windows]
    assert expected == [[policy], *([[], [policy]] if boundary == 'starts_on' else [[policy], []])]
    data['_restriction_index_cache'] = {}
    seen = []
    original = validation.applicable_restriction_intervals

    def intervals(view, row, *args):
        seen.append(row['id'])
        yield from original(view, row, *args)

    monkeypatch.setattr(validation, 'applicable_restriction_intervals', intervals)
    for pid in data['people']:
        for (lo, hi), rows in zip(windows, expected):
            assert list(validation.blocking_restrictions(data, pid, shift, lo, hi)) == rows
    assert seen == [policy['id']] * 6
    sessions = data['_restriction_index_cache'][id(data['policies'])][6]
    assert len(sessions) == 3 and all(sessions[id(shift), lo, hi][0] is shift for lo, hi in windows)


@pytest.fixture(scope='module')
def department_session_work():
    data, _, _, _, _ = source(144, rules=12)
    for n, policy in enumerate(data['policies']):
        policy['definition']['scope'] = {'service_ids': [f'clinic-{n}']}
    data['eligibilities'] = [{**row, 'service_id': f'clinic-{n}'} for row in data['eligibilities'] for n in range(12)]
    data['_person_rows'] = validation.index_person_rows(data)
    data['_line_index_cache'] = {}
    calls = Counter()
    inside = False
    original_intervals, original_matches = validation.applicable_restriction_intervals, validation.session_matches

    def intervals(*args):
        nonlocal inside
        calls['intervals'] += 1
        inside = True
        try:
            yield from original_intervals(*args)
        finally:
            inside = False

    def matches(*args):
        calls['canonical_matches' if inside else 'session_prefilter'] += 1
        return original_matches(*args)

    results = []
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(validation, 'applicable_restriction_intervals', intervals)
        monkeypatch.setattr(validation, 'session_matches', matches)
        started = time.process_time()
        for n in range(96):
            shift, _, _ = duty(n)
            shift['service_ids'] = [f'clinic-{n % 12}']
            results.extend(evidence_module.candidate_evidence(data, pid, shift) for pid in data['people'])
        elapsed = time.process_time() - started
    print('restriction session work', {'people': 144, 'sessions': 96, 'rules': 12, 'cpu_seconds': elapsed, **calls})
    return results, calls, elapsed


def test_department_session_predicates_are_indexed_once_not_per_candidate(department_session_work):
    results, calls, _ = department_session_work
    assert len(results) == 144 * 96
    for n, row in enumerate(results):
        assert row['exclusions'] == [{'code': 'RESTRICTION', 'rule_id': f'restriction-{n // 144 % 12}', 'rule_revision': 1}]
    assert calls['session_prefilter'] == 96 * 12
    assert calls['intervals'] == calls['canonical_matches'] == 96 * 144


@pytest.mark.cpu_budget
def test_department_session_index_cpu_budget(department_session_work):
    assert department_session_work[2] < python_budget(1.5), department_session_work[2]


def test_complete_prepare_model_evidence_score_program_and_raw_before_score_match_reference(monkeypatch):
    data = department()
    people = list(data['people'])[:6]
    data['people'] = {pid: data['people'][pid] for pid in people}
    data['shifts'] = [data['shifts'][n] for n in (0, 1, 15, 16)]
    for shift in data['shifts']:
        shift.update(participants=[], locked=False)
    data['focus_ids'] = [shift['id'] for shift in data['shifts']]
    data['availability'] = []
    for name in ('memberships', 'eligibilities'):
        data[name] = [row for row in data[name] if row['person_id'] in people]
    restriction = copy.deepcopy(data['policies'][32])
    restriction['definition']['scope'] = {'service_ids': data['shifts'][1]['service_ids']}
    restriction['definition']['rule'].update(weekdays=[4], start_time='09:00', end_time='10:00')
    advisory = copy.deepcopy(restriction)
    advisory.update(id='00000000-0000-4000-8000-000000000098', name='Synthetic advisory restriction')
    advisory['definition']['severity'] = 'advisory'
    advisory['definition']['rule']['weekdays'] = list(range(7))
    data['policies'] = [restriction, advisory]
    data['fingerprint'] = 'synthetic-complete-session-restriction-preparation'

    def prepare():
        prepared = cp_sat._prepare(data, 1, 1, SolverControl(), GenerationOptions(), False, True)
        assert 'model' in prepared, prepared
        assert not prepared['model'].validate()
        assert prepared['score_program'] and prepared['evidence'] and prepared['before_score']
        values = {'model': str(prepared['model'].proto),
                  **{key: prepared[key] for key in ('evidence', 'score_program', 'before_score')}}
        return {key: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
                for key, value in values.items()}

    actual = prepare()
    original = validation.blocking_restrictions

    def canonical(view, *args):
        yield from original(uncached(view), *args)

    monkeypatch.setattr(validation, 'blocking_restrictions', canonical)
    monkeypatch.setattr(evidence_module, 'blocking_restrictions', canonical)
    expected = prepare()
    assert actual == expected
    print('complete restriction preparation hashes', json.dumps(actual, sort_keys=True))
    assert '_restriction_index_cache' not in data and '_line_index_cache' not in data
