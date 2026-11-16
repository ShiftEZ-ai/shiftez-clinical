"""Quota rules that repeat: back-to-back weekly blocks and calendar months, as one rule."""
from packages.domain.quotas import applicable_policies


def data(rule, starts, ends):
    policy = {'id': 'q', 'status': 'active', 'active': True, 'department_id': 'd',
              'definition': {'scope': {}, 'severity': 'mandatory', 'rule': {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'minimum', 'amount': 1, **rule}}}
    return {'organisation': {'week_start': 6}, 'workspace': {'department_id': 'd', 'starts_on': starts, 'ends_on': ends}, 'policies': [policy]}


def windows(rule, starts, ends):
    return [(p['definition']['rule']['starts_on'], p['definition']['rule']['ends_on']) for p in applicable_policies(data(rule, starts, ends))]


def test_fixed_weeks_from_a_sunday():
    rule = {'starts_on': '2026-10-04', 'ends_on': '2026-11-01', 'window_days': 7, 'window_alignment': 'fixed'}
    assert windows(rule, '2026-10-11', '2026-10-25') == [('2026-10-11', '2026-10-18'), ('2026-10-18', '2026-10-25')]


def test_calendar_months():
    rule = {'starts_on': '2026-10-01', 'ends_on': '2027-01-01', 'window_alignment': 'calendar_month'}
    assert windows(rule, '2026-10-25', '2026-11-08') == [('2026-10-01', '2026-11-01'), ('2026-11-01', '2026-12-01')]


def test_rolling_is_unchanged():
    rule = {'starts_on': '2026-10-04', 'ends_on': '2026-11-01', 'window_days': 7}
    assert len(windows(rule, '2026-10-11', '2026-10-12')) == 7


def test_calendar_weeks_start_on_the_department_first_weekday_and_cut_at_the_edges():
    # Week starts Sunday (6). The rule starts on a Tuesday, so its first week is 1-6 Sep.
    rule = {'starts_on': '2026-09-01', 'ends_on': '2026-09-20', 'window_alignment': 'calendar_week'}
    assert windows(rule, '2026-09-01', '2026-09-20') == [('2026-09-01', '2026-09-06'), ('2026-09-06', '2026-09-13'), ('2026-09-13', '2026-09-20')]
