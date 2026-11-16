import pytest
from packages.domain.generation import generate


@pytest.mark.parametrize('start,end,overtime,blocked', [
    ('2026-10-09T08:00:00+03:00', '2026-10-09T12:00:00+03:00', True, True),
    ('2026-10-10T08:00:00+03:00', '2026-10-10T12:00:00+03:00', True, True),
    ('2026-10-16T08:00:00+03:00', '2026-10-16T12:00:00+03:00', True, False),
    ('2026-10-08T08:00:00+03:00', '2026-10-08T12:00:00+03:00', True, False),
    ('2026-10-08T23:00:00+03:00', '2026-10-09T03:00:00+03:00', True, True),
    ('2026-10-09T08:00:00+03:00', '2026-10-09T12:00:00+03:00', False, False),
])
def test_weekends_count_once_cross_period_and_respect_overtime_condition(h, start, end, overtime, blocked):
    h.policy({'kind': 'max_consecutive_weekends', 'maximum': 1, 'weekend_start': 4,
              'weekend_days': 2, 'overtime_only': True})
    prior = h.shift(h.workspace(start='2026-10-02', end='2026-10-04'),
        '2026-10-02T08:00:00+03:00', '2026-10-02T12:00:00+03:00')['workspace']
    prior = h.shift(prior, '2026-10-03T08:00:00+03:00', '2026-10-03T12:00:00+03:00')['workspace']
    h.publish(prior)
    result = h.shift(h.workspace(start='2026-10-04', end='2026-10-18'), start, end,
        people=[{'person_id': h.seed['people'][0], 'start': start, 'end': end, 'overtime': overtime}])
    assert ('CONSECUTIVE_WEEKENDS' in {f['code'] for f in result['validation']['findings']}) is blocked
    if blocked:
        h.lifecycle(result['workspace'], 'submit', status=422)
        data = h.snapshot(result['workspace'])
        current = next(s for s in data['shifts'] if s['id'] in data['focus_ids'])
        current['participants'] = []
        data['policies'][0]['definition']['rule']['overtime_only'] = False
        candidate = generate(data)
        assert candidate['status'] == 'candidate_feasible'
        assert candidate['additions'] and all(a['participant']['person_id'] != h.seed['people'][0] for a in candidate['additions'])
    else:
        h.publish(result['workspace'])


def test_leave_adjacent_weekend_uses_calendar_days_and_shared_allocation(h):
    import copy
    from packages.domain.validation import evaluate
    person = h.seed['people'][0]
    h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'recorded_leave',
        'start': '2026-10-05T00:00:00+03:00', 'end': '2026-10-06T00:00:00+03:00',
        'private_reason': 'Private source leave'})
    h.policy({'kind': 'restriction', 'weekdays': [4, 5], 'leave_buffer_days': 3}, {'service_id': h.seed['services'][0]})
    w = h.shift(h.workspace(start='2026-10-02', end='2026-10-03'), start='2026-10-02T08:00:00+03:00',
        end='2026-10-02T12:00:00+03:00', seed_restricted=True)['workspace']
    data = h.snapshot(w)
    result = evaluate(data)
    assert any(f['code'] == 'RESTRICTION' and f['person_id'] == person for f in result['findings'])
    assert 'Private source leave' not in str(result)
    h.lifecycle(w, 'submit', status=422)
    non_leave = copy.deepcopy(data)
    non_leave['availability'][0]['kind'] = 'external_commitment'
    assert not any(f['code'] == 'RESTRICTION' for f in evaluate(non_leave)['findings'])
    data['shifts'][0]['participants'] = []
    generated = generate(data)
    assert generated['status'] == 'candidate_feasible'
    assert generated['additions'] and all(a['participant']['person_id'] != person for a in generated['additions'])
    # The following Friday is four dates after the last leave day; it is outside the buffer.
    later = h.shift(h.workspace(start='2026-10-09', end='2026-10-10'), start='2026-10-09T08:00:00+03:00',
        end='2026-10-09T12:00:00+03:00')
    assert not any(f['code'] == 'RESTRICTION' for f in later['validation']['findings'])
    h.publish(later['workspace'])


@pytest.mark.postgres
def test_leave_adjacent_weekend_postgres(pg):
    test_leave_adjacent_weekend_uses_calendar_days_and_shared_allocation(pg)
