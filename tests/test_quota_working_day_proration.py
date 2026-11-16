"""Partial lower bounds use configured working days, preserving nominal counting windows."""
import copy

import pytest

from packages.contracts.schemas import DateSetInput, OperatingCalendarInput, PolicyDefinition
from packages.domain.validation import evaluate
from .test_finding_causes import cause_data, cause_snapshot, type_definition, uid


@pytest.fixture
def working_data(cause_data):
    data, pid = cause_data
    data['organisation']['week_start'] = 6
    data['workspace'].update(starts_on='2026-09-27', ends_on='2026-10-04')
    data['shifts'][0].update(start='2026-10-01T08:00:00+03:00', end='2026-10-01T12:00:00+03:00')
    data['quota_context'].update(start='2026-09-01T00:00:00+03:00', end='2026-11-01T00:00:00+03:00')
    calendar_id = uid('department-working-calendar')
    calendar = OperatingCalendarInput.model_validate({'name': 'Configured department working days',
        'starts_on': '2020-01-01', 'definition': {'timezone': 'Asia/Riyadh', 'week': [
            {'weekday': day, 'windows': [{'start_time': '00:00', 'end_time': '24:00'}]}
            for day in (6, 0, 1, 2, 3)]}}).model_dump(mode='json')
    data['operating_calendars'][calendar_id] = {'id': calendar_id,
        'department_id': data['workspace']['department_id'], 'revision': 1, **calendar}
    data['department'] = {'id': data['workspace']['department_id'], 'operating_calendar_id': calendar_id,
                          'weekend_days': [4, 5]}
    return data, pid


def add_quota(data, pid, *, scope=None, **changes):
    body = {'kind': 'block_quota', 'starts_on': '2026-09-27', 'ends_on': '2026-10-04',
            'window_alignment': 'calendar_week', 'metric': 'duties', 'comparison': 'minimum', 'amount': 6, **changes}
    definition = PolicyDefinition.model_validate({'rule': body, 'scope': {'person_id': pid, **(scope or {})},
        'severity': 'advisory' if body['comparison'] == 'target' else 'mandatory'}).model_dump(mode='json')
    row = {'id': uid('working-day-quota'), 'name': 'Constructed working-day quota',
        'department_id': data['workspace']['department_id'], 'revision': 1,
        'status': 'active', 'active': True, 'starts_on': '2020-01-01', 'ends_on': None,
        'kind': 'block_quota', 'definition': definition}
    data['policies'] = [row]
    return row


def checked_row(data):
    checked = evaluate(data)
    assert checked['quota_report']['complete'], checked
    [row] = checked['quota_report']['rows']
    return checked, row


def assert_proration(data, row, *, days, full=5, amount=2, configured=6,
                     first='2026-09-27', last='2026-10-04'):
    assert row['amount'] == amount
    assert row['proration'] == {'configured_amount': configured, 'amount': amount,
        'working_days': days, 'full_working_days': full, 'starts_on': first, 'ends_on': last,
        'calendar_id': data['department']['operating_calendar_id'], 'rounding': 'up'}


@pytest.mark.parametrize('first,last,days,amount', [
    ('2026-10-01', '2026-10-04', 1, 2), ('2026-09-27', '2026-10-01', 4, 5)])
def test_partial_rule_week_uses_full_department_week_and_rounds_up(working_data, first, last, days, amount):
    data, pid = working_data
    add_quota(data, pid, starts_on=first, ends_on=last)
    _, row = checked_row(data)
    assert (row['starts_on'], row['ends_on']) == (first, last)
    assert_proration(data, row, days=days, amount=amount)


def test_rota_boundary_prorates_against_the_complete_week(working_data):
    data, pid = working_data
    data['workspace']['starts_on'] = '2026-10-01'
    add_quota(data, pid)
    before = copy.deepcopy(data)
    _, row = checked_row(data)
    assert data == before
    assert_proration(data, row, days=1)


@pytest.mark.parametrize('dimension', ['membership', 'programme', 'rotation', 'external_rotation'])
def test_person_scope_dates_select_applicable_working_days(working_data, dimension):
    data, pid = working_data
    scope = {}
    if dimension == 'membership':
        for membership in data['memberships']:
            if membership['person_id'] == pid:
                membership['starts_on'] = '2026-10-01'
    elif dimension == 'programme':
        programme_id = uid('working-programme')
        data['programmes'][programme_id] = {'id': programme_id, 'active': True, 'starts_on': '2020-01-01', 'ends_on': None}
        data['programme_memberships'] = [{'id': uid('working-programme-membership'), 'person_id': pid,
            'programme_id': programme_id, 'starts_on': '2026-10-01', 'ends_on': '2026-10-04'}]
        scope['programme_id'] = programme_id
    else:
        rotation_type, cohort_id = uid('working-rotation-type'), uid('working-cohort')
        data['rotation_types'][rotation_type] = {'id': rotation_type, 'active': True, 'starts_on': '2020-01-01', 'ends_on': None}
        data['cohorts'][cohort_id] = {'id': cohort_id, 'active': True, 'starts_on': '2020-01-01', 'ends_on': None}
        data['rotations'] = [{'id': uid('working-rotation'), 'person_id': pid, 'cohort_id': cohort_id,
            'rotation_type_id': rotation_type, 'department_id': data['workspace']['department_id'],
            'service_id': data['shifts'][0]['service_ids'][0],
            'starts_on': '2026-09-27' if dimension == 'external_rotation' else '2026-10-01',
            'ends_on': '2026-10-01' if dimension == 'external_rotation' else '2026-10-04',
            'department_available': dimension != 'external_rotation'}]
        if dimension == 'rotation':
            scope['rotation_type_id'] = rotation_type
    add_quota(data, pid, scope=scope)
    _, row = checked_row(data)
    assert_proration(data, row, days=1)


@pytest.mark.parametrize('metric,comparison,amount', [('duties', 'minimum', 6),
    ('duties', 'target', 2.5), ('session_units', 'target', 1.25), ('scheduled_hours', 'minimum', 1.25)])
@pytest.mark.parametrize('selected_calendar', [True, False])
def test_full_windows_preserve_exact_configured_amount_without_requiring_calendar(working_data, metric, comparison, amount, selected_calendar):
    data, pid = working_data
    if not selected_calendar:
        data['department']['operating_calendar_id'] = None
    add_quota(data, pid, metric=metric, comparison=comparison, amount=amount)
    _, row = checked_row(data)
    assert row['amount'] == amount and not row.get('proration')


@pytest.mark.parametrize('comparison,metric,amount,expected', [('minimum', 'duties', 6, 2),
    ('target', 'duties', 6, 2), ('target', 'session_units', 1.25, 1), ('minimum', 'scheduled_hours', 1.25, 1)])
@pytest.mark.parametrize('selected_calendar', [True, False])
def test_every_partial_minimum_and_target_uses_ceiling(working_data, comparison, metric, amount, expected, selected_calendar):
    data, pid = working_data
    if not selected_calendar:
        data['department']['operating_calendar_id'] = None
    add_quota(data, pid, starts_on='2026-10-01', comparison=comparison, metric=metric, amount=amount)
    _, row = checked_row(data)
    assert_proration(data, row, days=1, configured=amount, amount=expected)


@pytest.mark.parametrize('weekend,days,full,amount', [([4, 5], 1, 5, 2), ([5, 6], 2, 5, 3),
    ([], 3, 7, 3), ([0, 1, 2, 3, 4, 5], 0, 1, 0)])
def test_unselected_calendar_uses_department_weekends_without_inference(working_data, weekend, days, full, amount):
    data, pid = working_data
    data['department'].update(operating_calendar_id=None, weekend_days=weekend)
    # Other calendars and generic date sets are not department holiday selections.
    data['date_sets'][uid('unselected-holidays')] = {'id': uid('unselected-holidays'), 'name': 'Public holidays',
        'active': True, 'entries': [{'id': uid('unselected-holiday'), 'name': 'Synthetic unselected holiday', 'first_day': '2026-10-01',
                                    'last_day': '2026-10-03', 'repeat': 'none'}]}
    data['workspace']['starts_on'] = '2026-10-01'
    add_quota(data, pid)
    _, row = checked_row(data)
    assert_proration(data, row, days=days, full=full, amount=amount)


def test_selected_calendar_overrides_different_department_weekends(working_data):
    data, pid = working_data
    data['department']['weekend_days'] = [5, 6]
    data['workspace']['starts_on'] = '2026-10-01'
    add_quota(data, pid)
    _, row = checked_row(data)
    assert_proration(data, row, days=1)


@pytest.mark.parametrize('legacy_partial_flag', [False, True])
def test_maximum_is_never_scaled_for_partial_windows(working_data, legacy_partial_flag):
    data, pid = working_data
    data['department']['operating_calendar_id'] = None
    add_quota(data, pid, starts_on='2026-10-01', comparison='maximum', window_alignment='fixed',
              window_days=7, window_anchor_on='2026-09-27', prorate_partial_windows=legacy_partial_flag)
    _, row = checked_row(data)
    assert row['amount'] == 6 and not row.get('proration')


@pytest.mark.parametrize('source,full,amount', [('override', 4, 3), ('date_set', 4, 3), ('reopened', 5, 2), ('unreferenced', 5, 2)])
def test_only_configured_holiday_closures_change_working_days(working_data, source, full, amount):
    data, pid = working_data
    calendar = data['operating_calendars'][data['department']['operating_calendar_id']]['definition']
    holiday_id = uid('working-holiday')
    holiday = DateSetInput(name='Public holidays', entries=[{'id': uid('holiday-entry'),
        'name': 'Synthetic holiday', 'first_day': '2026-09-28', 'last_day': '2026-09-28'}]).model_dump(mode='json')
    data['date_sets'][holiday_id] = {'id': holiday_id, **holiday}
    if source == 'override':
        calendar['overrides'] = [{'on': '2026-09-28', 'windows': []}]
    elif source != 'unreferenced':
        calendar['date_set_rules'] = [{'date_set_id': holiday_id, 'windows': []}]
    if source == 'reopened':
        calendar['overrides'] = [{'on': '2026-09-28', 'windows': [{'start_time': '10:00', 'end_time': '11:00'}]}]
    add_quota(data, pid, starts_on='2026-09-30', amount=5)
    _, row = checked_row(data)
    assert_proration(data, row, days=2, full=full, configured=5, amount=amount)


def test_leave_does_not_reduce_partial_window_commitment(working_data):
    data, pid = working_data
    data['availability'] = [{'id': uid('working-leave'), 'person_id': pid, 'kind': 'approved_leave',
        'start': '2026-09-27T00:00:00+03:00', 'end': '2026-10-04T00:00:00+03:00'}]
    add_quota(data, pid, starts_on='2026-10-01')
    _, row = checked_row(data)
    assert_proration(data, row, days=1)


def test_full_window_off_duty_prorates_mandatory_minimum_to_zero_for_schedulers(working_data):
    from packages.domain import cp_sat

    data, pid = working_data
    data['availability'] = [{'id': uid('working-off-duty'), 'person_id': pid, 'kind': 'off_duty',
        'counts_as_unavailable': True, 'start': '2026-09-27T00:00:00+03:00', 'end': '2026-10-04T00:00:00+03:00'}]
    type_definition(data, requirements=[])
    add_quota(data, pid)
    checked, row = checked_row(data)
    assert row['amount'] == 0 and row['status'] == 'met'
    assert (row['proration']['working_days'], row['proration']['full_working_days']) == (0, 5)
    assert not any(f['code'] == 'QUOTA_BELOW_MINIMUM' for f in checked['findings'])
    result = cp_sat.generate(data, time_budget_seconds=10)
    assert result['status'] == 'candidate_feasible', result
    [solver_row] = result['validation']['quota_report']['rows']
    assert solver_row['amount'] == 0 and solver_row['status'] == 'met'


@pytest.mark.parametrize('partial', [False, True])
def test_duty_weekdays_and_clocks_do_not_reduce_person_time_in_scope(working_data, partial):
    data, pid = working_data
    if partial:
        data['workspace']['starts_on'] = '2026-10-01'
    add_quota(data, pid, scope={'weekdays': [3], 'start_time': '08:00', 'end_time': '12:00'})
    _, row = checked_row(data)
    if partial:
        assert_proration(data, row, days=1)
    else:
        assert row['amount'] == 6 and not row.get('proration')


def test_policy_validity_shortens_scope_without_restarting_the_week(working_data):
    data, pid = working_data
    policy = add_quota(data, pid)
    policy['starts_on'] = '2026-10-01'
    _, row = checked_row(data)
    assert_proration(data, row, days=1)


def test_opportunity_cache_keeps_separate_policy_validity_and_prefills(working_data):
    data, pid = working_data
    data['shifts'][0].update(start='2026-09-27T08:00:00+03:00', end='2026-09-27T12:00:00+03:00')
    first = add_quota(data, pid, amount=1)
    second = copy.deepcopy(first)
    second.update(id=uid('later-validity-quota'), starts_on='2026-10-01')
    data['policies'] = [first, second]
    checked = evaluate(data)
    findings = {row['rule_id']: row for row in checked['findings'] if row['code'] == 'QUOTA_BELOW_MINIMUM'}
    assert findings[first['id']]['cause']['sessions_found'] == 1
    # The only candidate duty is before this rule becomes effective.
    assert findings[second['id']]['cause']['sessions_found'] == 0
    hint = findings[second['id']]['fix_hints'][0]
    assert hint['prefill']['starts_on'] == '2026-10-01' and hint['prefill']['shift_ids'] == []


def test_no_working_day_overlap_calculates_zero_without_a_breach(working_data):
    data, pid = working_data
    data['workspace'].update(starts_on='2026-10-02', ends_on='2026-10-04')
    data['shifts'], data['focus_ids'] = [], []
    add_quota(data, pid)
    checked, row = checked_row(data)
    assert_proration(data, row, days=0, amount=0)
    assert row['status'] == 'met'
    assert not any(f['code'] == 'QUOTA_BELOW_MINIMUM' for f in checked['findings'])


def test_no_sessions_cause_retains_partial_window_proration(working_data):
    data, pid = working_data
    data['workspace']['starts_on'] = '2026-10-01'
    data['shifts'], data['focus_ids'] = [], []
    add_quota(data, pid)
    checked, row = checked_row(data)
    assert_proration(data, row, days=1)
    finding = next(f for f in checked['findings'] if f['code'] == 'QUOTA_BELOW_MINIMUM')
    assert finding['cause']['code'] == 'partial_window'
    assert finding['cause']['required'] == 2 and finding['cause']['sessions_found'] == 0
    assert finding['cause']['proration'] == row['proration']
    assert [hint['action'] for hint in finding['fix_hints']] == ['add_sessions']
    prefill = finding['fix_hints'][0]['prefill']
    assert prefill['starts_on'] == data['workspace']['starts_on']
    assert prefill['ends_on'] == data['workspace']['ends_on']


@pytest.mark.parametrize('missing', ['department', 'weekend_days'])
def test_historical_snapshot_without_weekends_uses_default_working_days(working_data, missing):
    data, pid = working_data
    data['department']['operating_calendar_id'] = None
    add_quota(data, pid, starts_on='2026-10-01')
    if missing == 'department':
        data.pop('department')
    else:
        data['department'].pop('weekend_days')
    _, row = checked_row(data)
    # Older snapshots lack the new selection fields and retain the department's default Friday/Saturday weekend.
    assert row['amount'] == 2 and row['proration']['working_days'] == 1
    assert row['proration']['full_working_days'] == 5 and row['proration']['calendar_id'] is None


@pytest.mark.parametrize('problem', ['invalid_weekends', 'missing_calendar',
                                     'inactive_calendar', 'foreign_calendar', 'zero_days', 'calendar_error'])
def test_partial_proration_fails_closed_when_working_days_cannot_be_resolved(working_data, problem):
    data, pid = working_data
    add_quota(data, pid, starts_on='2026-10-01')
    if problem == 'invalid_weekends':
        data['department'].update(operating_calendar_id=None, weekend_days=[7])
    elif problem == 'missing_calendar':
        data['department']['operating_calendar_id'] = uid('missing-calendar')
    elif problem == 'zero_days':
        data['operating_calendars'][data['department']['operating_calendar_id']]['definition']['week'] = []
    elif problem in {'inactive_calendar', 'foreign_calendar'}:
        calendar = data['operating_calendars'][data['department']['operating_calendar_id']]
        calendar.update({'active': False} if problem == 'inactive_calendar' else {'department_id': uid('foreign')})
    else:
        data['workspace'].update(starts_on='2080-10-01', ends_on='2080-10-04')
        data['quota_context'].update(start='2080-09-01T00:00:00+03:00', end='2080-11-01T00:00:00+03:00')
        add_quota(data, pid, starts_on='2080-10-01', ends_on='2080-10-04')
        holiday_id = uid('unresolved-hijri-holiday')
        data['date_sets'][holiday_id] = {'id': holiday_id, 'name': 'Configured Hijri holidays', 'active': True,
            'entries': [{'id': uid('hijri-entry'), 'name': 'Holiday', 'repeat': 'yearly_hijri',
                         'first_day': '1447-10-01', 'last_day': '1447-10-04'}]}
        data['operating_calendars'][data['department']['operating_calendar_id']]['definition']['date_set_rules'] = [
            {'date_set_id': holiday_id, 'windows': []}]
        data['shifts'], data['focus_ids'] = [], []
    checked = evaluate(data)
    assert not checked['quota_report']['complete']
    assert not checked['valid']
    assert any(f['code'] == 'QUOTA_PRORATION_UNRESOLVED' for f in checked['findings'])


@pytest.mark.parametrize('alignment', ['calendar_week', 'fixed'])
def test_repeating_windows_keep_phase_and_never_scale_twice(working_data, alignment):
    data, pid = working_data
    data['workspace'].update(starts_on='2026-10-01', ends_on='2026-10-11')
    options = {'window_days': 7, 'window_anchor_on': '2026-09-27', 'prorate_partial_windows': True} if alignment == 'fixed' else {}
    add_quota(data, pid, starts_on='2026-10-01', ends_on='2026-10-18', repeat=True,
              window_alignment=alignment, **options)
    checked = evaluate(data)
    assert checked['quota_report']['complete']
    rows = checked['quota_report']['rows']
    assert len(rows) == 2
    partial = next(row for row in rows if row['starts_on'] == '2026-10-01')
    full = next(row for row in rows if row['starts_on'] == '2026-10-04')
    assert_proration(data, partial, days=1)
    assert full['amount'] == 6 and not full.get('proration')


def test_calendar_month_proration_retains_the_complete_month(working_data):
    data, pid = working_data
    data['workspace'].update(starts_on='2026-10-01', ends_on='2026-11-01')
    add_quota(data, pid, starts_on='2026-10-29', ends_on='2026-11-01', window_alignment='calendar_month')
    _, row = checked_row(data)
    assert_proration(data, row, days=1, full=21, amount=1, first='2026-10-01', last='2026-11-01')


def test_catalogue_validity_clipping_keeps_original_counting_window(working_data):
    from .test_rule_lines import line, person, rule

    data, pid = working_data
    data['policies'] = [rule('duties_per_week', line(who=person(pid), starts_on='2026-10-01',
        ends_on='2026-10-04', numbers={'min': {'value': 6, 'level': 'mandatory'}}),
        settings={'block_quota': {'starts_on': '2026-09-27', 'ends_on': '2026-10-04',
                                  'window_alignment': 'calendar_week'}},
        department_id=data['workspace']['department_id'])]
    _, row = checked_row(data)
    assert (row['starts_on'], row['ends_on']) == ('2026-10-01', '2026-10-04')
    assert_proration(data, row, days=1)


def test_partial_block_cannot_be_grouped_with_a_week_having_a_different_denominator(working_data):
    from packages.domain.rule_groups import plan

    data, pid = working_data
    weekly = add_quota(data, pid, starts_on='2026-10-01')
    weekly.update(owner='Synthetic owner', source='Constructed grouping regression')
    block = copy.deepcopy(weekly)
    block.update(id=uid('explicit-partial-block'))
    block['definition']['rule']['window_alignment'] = 'rolling'
    data['policies'] = [weekly, block]
    checked = evaluate(data)
    assert checked['quota_report']['complete']
    assert {row['policy_id']: row['amount'] for row in checked['quota_report']['rows']} == {
        weekly['id']: 2, block['id']: 6}
    groups = plan(data['policies'], week_start=6)
    assert not any(set(group['rule_ids']) == {weekly['id'], block['id']} for group in groups)


def test_full_explicit_rate_target_keeps_existing_calendar_day_calculation(working_data):
    data, pid = working_data
    add_quota(data, pid, comparison='target', window_alignment='rolling',
              prorate_department_days=True, reference_days=28, rounding='nearest')
    _, row = checked_row(data)
    assert row['amount'] == 2 and not row.get('proration')
    assert row['target_calculation']['department_days'] == 7
    assert row['target_calculation']['unrounded_amount'] == 1.5
    assert row['target_calculation']['calculated_amount'] == 2


@pytest.mark.parametrize('selected_calendar', [True, False])
def test_round_down_rate_target_still_rounds_its_partial_working_day_share_up(working_data, selected_calendar):
    data, pid = working_data
    if not selected_calendar:
        data['department']['operating_calendar_id'] = None
    add_quota(data, pid, comparison='target', window_alignment='rolling',
              prorate_department_days=True, reference_days=28, rounding='down')
    _, full = checked_row(data)
    assert full['amount'] == 1 and not full.get('proration')  # floor(6 * 7/28)
    data['workspace']['starts_on'] = '2026-10-01'
    _, partial = checked_row(data)
    assert_proration(data, partial, days=1, configured=1, amount=1)  # ceil(1 * 1/5)
    assert partial['target_calculation'] == full['target_calculation']
    assert partial['target_calculation']['rounding'] == 'down'


@pytest.mark.parametrize('override,expected', [(None, 2), (4, 4)])
def test_explicit_rate_target_scales_full_window_once_and_preserves_override(working_data, override, expected):
    data, pid = working_data
    for membership in data['memberships']:
        if membership['person_id'] == pid:
            membership['starts_on'] = '2026-10-01'
    row = add_quota(data, pid, comparison='target', window_alignment='rolling',
                    prorate_department_days=True, reference_days=7, rounding='nearest')
    if override is not None:
        data['workspace']['target_overrides'] = [{'policy_id': row['id'], 'policy_revision': row['revision'],
            'person_id': pid, 'amount': override, 'reason': 'Existing approved target adjustment'}]
    _, report_row = checked_row(data)
    assert report_row['amount'] == expected
    assert report_row['target_calculation']['department_days'] == 7
    assert report_row['target_calculation']['calculated_amount'] == 6
    if override is None:
        assert_proration(data, report_row, days=1)
    else:
        assert report_row['target_calculation']['override_amount'] == override


@pytest.mark.parametrize('selected_calendar', [True, False])
def test_rule_preview_and_report_display_use_the_same_proration(working_data, selected_calendar):
    from packages.domain.quotas import project_report
    from packages.domain.rule_map import reach
    from packages.domain.time import aware

    data, pid = working_data
    if not selected_calendar:
        data['department']['operating_calendar_id'] = None
    data['workspace']['starts_on'] = '2026-10-01'
    policy = add_quota(data, pid)
    checked, report_row = checked_row(data)
    expected = '0 of 2 (prorated: 1 of 5 days)'
    assert report_row['display'] == expected
    [displayed] = project_report(checked['quota_report'], pid)['rows']
    assert displayed['display'] == expected and displayed['proration'] == report_row['proration']
    details = reach(data, data['policies'], aware('2026-10-01T00:00:00+03:00'),
                    aware('2026-10-04T00:00:00+03:00'), details=True)['details'][policy['id']]
    [line] = details['lines']
    [person] = line['people']
    [count] = person['counts']
    assert person['person_id'] == pid
    assert (count['amount'], count['actual'], count['display']) == (2, 0, expected)
    assert count['proration'] == report_row['proration']


@pytest.mark.parametrize('engine', ['greedy', 'cp_sat'])
@pytest.mark.parametrize('selected_calendar', [True, False])
def test_both_schedulers_use_the_shared_prorated_row_amount(working_data, engine, selected_calendar):
    from packages.domain import cp_sat, generation

    data, pid = working_data
    if not selected_calendar:
        data['department']['operating_calendar_id'] = None
    data['workspace']['starts_on'] = '2026-10-01'
    type_definition(data, requirements=[])
    first = data['shifts'][0]
    second = copy.deepcopy(first)
    second.update(id=uid('afternoon-proration-duty'), start='2026-10-01T13:00:00+03:00', end='2026-10-01T17:00:00+03:00')
    data['shifts'].append(second)
    data['focus_ids'].append(second['id'])
    add_quota(data, pid)
    result = (generation if engine == 'greedy' else cp_sat).generate(data, time_budget_seconds=10)
    assert result['status'] == 'candidate_feasible', result
    [row] = result['validation']['quota_report']['rows']
    assert row['amount'] == 2 and row['actual'] == 2 and row['status'] == 'met'
    assert len(result['additions']) == 2
