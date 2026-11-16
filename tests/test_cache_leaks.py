from datetime import datetime, timezone
from copy import deepcopy

import pytest

from packages.domain.candidate_evidence import _absence_exclusions
from packages.domain.validation import applicable_restriction_intervals


def test_candidate_absence_cache_does_not_reuse_after_leave_is_recorded():
    start = datetime(2026, 10, 4, 8, tzinfo=timezone.utc)
    end = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    source = {'organisation': {'timezone': 'UTC'}, 'rotations': [], 'availability': []}
    first = {**source, '_candidate_absence_cache': {}}
    assert _absence_exclusions(first, 'p', start, end) == []
    changed = {**source, 'availability': [{
        'id': 'leave', 'person_id': 'p', 'start': start.isoformat(), 'end': end.isoformat()}],
        '_candidate_absence_cache': {}}
    assert _absence_exclusions(changed, 'p', start, end) == [{
        'code': 'UNAVAILABLE', 'record_id': 'leave', 'start': start.isoformat(), 'end': end.isoformat()}]


def test_restriction_interval_cache_does_not_reuse_after_leave_is_recorded():
    start = datetime(2026, 10, 5, 8, tzinfo=timezone.utc)
    end = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    shift = {'id': 's', 'assignment_type_id': 't', 'service_ids': [], 'location_id': None, 'participants': []}
    policy = {'id': 'r', 'status': 'active', 'active': True, 'department_id': 'd',
              'starts_on': None, 'ends_on': None,
              'definition': {'scope': {}, 'severity': 'mandatory', 'rule': {
                  'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '08:00',
                  'end_time': '12:00', 'leave_buffer_days': 1}}}
    source = {'organisation': {'timezone': 'UTC'}, 'types': {'t': {'department_id': 'd'}},
              'policies': [policy], 'people': {'p': {'active': True}}, 'memberships': [],
              'availability': [], 'shifts': [shift], '_restriction_interval_cache': {}}
    assert list(applicable_restriction_intervals(source, policy, 'p', shift, start, end)) == []
    changed = {**source, 'availability': [{
        'id': 'leave', 'kind': 'approved_leave', 'person_id': 'p',
        'start': start.isoformat(), 'end': end.isoformat()}],
        '_restriction_interval_cache': {}}
    assert list(applicable_restriction_intervals(changed, policy, 'p', shift, start, end))


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_evaluate_owns_restriction_interval_cache_when_leave_buffer_reads_availability(h):
    from packages.domain import validation
    from tests.test_solver_supervision_cache import snapshot

    source, *_ = snapshot(h)
    source['policies'].append({
        'id': 'buffered-restriction', 'status': 'active', 'active': True,
        'department_id': source['workspace']['department_id'], 'starts_on': None, 'ends_on': None,
        'revision': 1,
        'definition': {'scope': {}, 'severity': 'mandatory', 'rule': {
            'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '08:00',
            'end_time': '12:00', 'leave_buffer_days': 1}},
    })
    caller_cache = {}
    source['_restriction_interval_cache'] = caller_cache
    original = deepcopy(source)
    validation.evaluate(source, explain_findings=False)
    assert source == original


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_evaluate_recomputes_candidate_absence_after_leave_is_recorded(h):
    from packages.domain import validation
    from tests.test_solver_supervision_cache import snapshot

    source, first, _, _, _, trainee, *_ = snapshot(h)
    source['_candidate_absence_cache'] = {}
    baseline = validation.evaluate(source)
    source['availability'].append({'id': 'new-leave', 'person_id': trainee,
                                   'kind': 'recorded_leave', 'start': first['start'], 'end': first['end']})
    changed = validation.evaluate(source)
    assert any(f['code'] == 'UNAVAILABLE' and f.get('person_id') == trainee
               for f in changed['findings'])
    assert changed != baseline
    assert source['_candidate_absence_cache'] == {}
