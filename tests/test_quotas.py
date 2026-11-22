"""Fractional quota proration never increases the configured commitment."""
import pytest

from packages.domain.quotas import project_report
from .test_finding_causes import cause_data, cause_snapshot, type_definition, uid
from .test_quota_working_day_proration import add_quota, checked_row, working_data


@pytest.fixture
def fractional_data(working_data):
    data, pid = working_data
    data['workspace'].update(starts_on='2026-09-28', ends_on='2026-10-11')
    type_definition(data, requirements=[], session_units=2.5)
    shift = data['shifts'][0]
    shift.update(start='2026-10-01T08:00:00+03:00', end='2026-10-01T10:30:00+03:00')
    shift['participants'] = [{'id': uid('fractional-proration-assignment'), 'person_id': pid,
        'shift_id': shift['id'], 'start': shift['start'], 'end': shift['end'],
        'participation': 'staff', 'overtime': False, 'team_lead': False, 'revision': 1}]
    return data, pid


def test_prorated_minimum_never_exceeds_unscaled(fractional_data):
    data, pid = fractional_data
    add_quota(data, pid, starts_on='2026-09-27', ends_on='2026-10-11',
              window_alignment='rolling', metric='scheduled_hours', amount=2.5)
    checked, row = checked_row(data)
    assert row['amount'] == 2.5
    assert (row['proration']['working_days'], row['proration']['full_working_days']) == (9, 10)
    assert (row['actual'], row['status']) == (2.5, 'met')
    assert checked['valid']
    assert project_report(checked['quota_report'], pid)['rows'] == [row]


@pytest.mark.parametrize('selected_calendar', [False, True])
@pytest.mark.parametrize('comparison,metric,amount', [
    ('minimum', 'reserved_hours', 2.5), ('minimum', 'session_units', 2.5),
    ('minimum', 'session_units', 0.25), ('target', 'duties', 2.5),
    ('target', 'session_units', 1.25), ('target', 'scheduled_hours', 2.5),
])
def test_partial_fractional_bounds_keep_their_original_ceiling(fractional_data, selected_calendar, comparison, metric, amount):
    data, pid = fractional_data
    if not selected_calendar:
        data['department']['operating_calendar_id'] = None
    if metric == 'session_units':
        type_definition(data, session_units=amount)
    add_quota(data, pid, starts_on='2026-09-27', ends_on='2026-10-11', window_alignment='rolling',
              comparison=comparison, metric=metric, amount=amount)
    checked, row = checked_row(data)
    assert row['amount'] == row['proration']['configured_amount'] == amount
    assert (row['proration']['working_days'], row['proration']['full_working_days']) == (9, 10)
    assert row['status'] == ('below_target' if metric == 'duties' else 'met')
    assert checked['valid']


@pytest.mark.parametrize('override,expected', [(None, 2.5), (4.25, 4.25)])
def test_fractional_rate_target_caps_the_calculated_amount_and_preserves_override(fractional_data, override, expected):
    data, pid = fractional_data
    policy = add_quota(data, pid, starts_on='2026-09-27', ends_on='2026-10-11', window_alignment='rolling',
        comparison='target', metric='session_units', amount=1.25,
        prorate_department_days=True, reference_days=7, rounding='nearest')
    if override is not None:
        _, target = checked_row(data)
        data['workspace']['target_overrides'] = [{'policy_id': policy['id'], 'policy_revision': policy['revision'],
            'person_id': pid, 'amount': override, 'reason': 'Synthetic exact target adjustment',
            'rule_fingerprints': [target['target_calculation']['rule_fingerprint']]}]
    _, row = checked_row(data)
    assert row['amount'] == expected
    assert row['target_calculation']['calculated_amount'] == 2.5
    if override is None:
        assert row['proration']['configured_amount'] == 2.5
    else:
        assert row['target_calculation']['override_amount'] == 4.25 and 'proration' not in row


@pytest.mark.parametrize('engine', ['greedy', 'cp_sat'])
@pytest.mark.parametrize('metric', ['scheduled_hours', 'session_units'])
def test_schedulers_and_reports_share_the_capped_fractional_minimum(fractional_data, engine, metric):
    from packages.domain import cp_sat, generation

    data, pid = fractional_data
    data['shifts'][0]['participants'] = []
    add_quota(data, pid, starts_on='2026-09-27', ends_on='2026-10-11',
              window_alignment='rolling', metric=metric, amount=2.5)
    result = (generation if engine == 'greedy' else cp_sat).generate(data, time_budget_seconds=5)
    assert result['status'] == 'candidate_feasible', result
    checked = result['validation']
    [row] = checked['quota_report']['rows']
    assert (row['actual'], row['amount'], row['status']) == (2.5, 2.5, 'met')
    assert project_report(checked['quota_report'], pid)['rows'] == [row]
