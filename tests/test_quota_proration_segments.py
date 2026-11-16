from datetime import date, timedelta
import math

from packages.domain.quotas import departmental_dates, window_proration
from .test_finding_causes import cause_data, cause_snapshot


MONTH_START = '2026-10-01'
MONTH_END = '2026-11-01'


def _month(data):
    data['workspace'].update(starts_on=MONTH_START, ends_on=MONTH_END)
    data['quota_context'].update(start='2026-10-01T00:00:00+03:00', end='2026-11-01T00:00:00+03:00')
    data['availability'] = []
    data['rotations'] = []


def _minimum_policy(pid):
    return {'definition': {'rule': {'starts_on': MONTH_START, 'ends_on': MONTH_END,
                                    'comparison': 'minimum', 'amount': 16},
                           'scope': {'person_id': pid}},
            '_quota_full_window': (MONTH_START, MONTH_END)}


def _off_duty(pid, first):
    start = date.fromisoformat(first)
    return {'person_id': pid, 'kind': 'off_duty', 'counts_as_unavailable': True,
            'start': f'{first}T00:00:00+03:00',
            'end': f'{(start + timedelta(days=1)).isoformat()}T00:00:00+03:00'}


def test_monthly_minimum_prorates_five_leave_days_by_actual_presence(cause_data):
    data, pid = cause_data
    _month(data)
    data['availability'] = [_off_duty(pid, f'2026-10-{day:02d}') for day in range(4, 9)]

    amount, proration = window_proration(data, _minimum_policy(pid), pid, 16, {}, None)

    assert proration['full_working_days'] == 21
    assert proration['working_days'] == 16
    assert amount == math.ceil(16 * (21 - 5) / 21) == 13


def test_monthly_minimum_prorates_one_leave_day_without_dropping_month(cause_data):
    data, pid = cause_data
    _month(data)
    data['availability'] = [_off_duty(pid, '2026-10-04')]

    amount, proration = window_proration(data, _minimum_policy(pid), pid, 16, {}, None)

    assert proration['full_working_days'] == 21
    assert proration['working_days'] == 20
    assert amount == math.ceil(16 * (21 - 1) / 21) == 16


def test_membership_end_date_limits_departmental_presence(cause_data):
    data, pid = cause_data
    _month(data)
    membership = next(row for row in data['memberships'] if row['person_id'] == pid)
    membership['ends_on'] = '2026-10-20'

    present = departmental_dates(data, pid, {}, MONTH_START, MONTH_END)

    assert len(present) == 19
    assert present[-1] == date(2026, 10, 19)


def test_mid_month_external_rotation_excludes_only_its_ten_days(cause_data):
    data, pid = cause_data
    _month(data)
    data['rotations'] = [{'person_id': pid, 'starts_on': '2026-10-10', 'ends_on': '2026-10-20',
                          'department_available': False, 'cohort_id': 'away-cohort',
                          'rotation_type_id': 'away-rotation', 'service_id': 'away-service'}]

    present = set(departmental_dates(data, pid, {}, MONTH_START, MONTH_END))

    assert len(present) == 21
    assert not present.intersection(date(2026, 10, 10) + timedelta(days=n) for n in range(10))
