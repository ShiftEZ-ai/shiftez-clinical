"""Working-day quota proration stays bounded for a department and retained rule history."""
import copy
import gc
import json
import time

import pytest

from packages.contracts.schemas import OperatingCalendarInput, PolicyDefinition
from packages.domain.validation import evaluate
from .test_finding_causes import uid
from .test_performance_budget import python_budget
from .test_quota_subject_performance import department_quota_preparation


@pytest.fixture(scope='module', params=['selected_calendar', 'department_weekends'])
def department_proration(department_quota_preparation, request):
    _, _, source, *_ = department_quota_preparation
    data = copy.deepcopy(source)
    data['rotations'], data['availability'] = [], []
    data['quota_context']['start'] = '2026-10-01T00:00:00+03:00'
    calendar_id = uid('scale-proration-calendar')
    calendar = OperatingCalendarInput.model_validate({'name': 'Department scale working days',
        'starts_on': '2020-01-01', 'definition': {'timezone': 'Asia/Riyadh', 'week': [
            {'weekday': day, 'windows': [{'start_time': '00:00', 'end_time': '24:00'}]}
            for day in (6, 0, 1, 2, 3)]}}).model_dump(mode='json')
    data['operating_calendars'][calendar_id] = {'id': calendar_id,
        'department_id': data['workspace']['department_id'], 'revision': 1, **calendar}
    data['department'] = {'id': data['workspace']['department_id'],
        'operating_calendar_id': calendar_id if request.param == 'selected_calendar' else None, 'weekend_days': [4, 5]}
    people = list(data['people'])
    expected = {pid: 20 if n < 75 else 1 for n, pid in enumerate(people)}
    for membership in data['memberships']:
        membership['starts_on'] = '2020-01-01' if expected[membership['person_id']] == 20 else '2026-10-29'
    for policy in data['policies']:
        for line in policy['definition']['rule']['lines']:
            line['numbers'] = {'min': {'value': 21, 'level': 'mandatory'}}
            line['settings'] = {'block_quota': {'metric': 'duties', 'starts_on': '2026-10-01',
                'ends_on': '2026-11-01', 'window_alignment': 'calendar_month'}}
        policy['definition'] = PolicyDefinition.model_validate(policy['definition']).model_dump(mode='json')
    active_ids = {policy['id'] for policy in data['policies']}
    history = PolicyDefinition.model_validate({'rule': {'kind': 'max_duties', 'maximum': 5}}).model_dump(mode='json')
    data['policies'].extend({'id': uid(f'proration-history-{n}'), 'name': f'Retained historical rule {n}',
        'department_id': data['workspace']['department_id'], 'kind': 'max_duties', 'revision': 1,
        'status': 'retired', 'active': False, 'starts_on': '2020-01-01', 'ends_on': '2026-01-01',
        'definition': history} for n in range(1000 - len(data['policies'])))
    before = copy.deepcopy(data)
    gc.collect()
    started = time.process_time()
    checked = evaluate(data, quota_only=True)
    elapsed = time.process_time() - started
    measurement = {'working_day_source': request.param,
        'people': len(people), 'sessions': len(data['shifts']), 'rules_including_history': len(data['policies']),
        'active_quota_sources': len(active_ids), 'active_catalogue_lines': sum(
            len(policy['definition']['rule'].get('lines', [])) for policy in data['policies'] if policy['id'] in active_ids),
        'quota_rows': len(checked['quota_report']['rows']), 'cpu_seconds': elapsed}
    print('department working-day proration', measurement)
    return data, before, checked, expected, active_ids, measurement


def test_department_working_day_proration_preserves_exact_amounts_and_history(department_proration, request):
    data, before, checked, expected, active_ids, measured = department_proration
    request.node.user_properties.append(('quota_proration_performance', json.dumps(measured)))
    assert measured['people'] == 150 and measured['sessions'] >= 500
    assert measured['rules_including_history'] == 1000 and measured['active_catalogue_lines'] >= 900
    assert measured['active_quota_sources'] == 6 and measured['quota_rows'] == 900
    assert data == before
    assert checked['quota_report']['complete'], checked
    rows = checked['quota_report']['rows']
    assert {(row['policy_id'], row['person_id']) for row in rows} == {
        (policy_id, pid) for policy_id in active_ids for pid in expected}
    for row in rows:
        days = expected[row['person_id']]
        assert row['amount'] == days and row['actual'] == 0 and row['status'] == 'below_minimum', row
        assert row['proration'] == {'configured_amount': 21, 'amount': days, 'working_days': days,
            'full_working_days': 21, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
            'calendar_id': data['department']['operating_calendar_id'], 'rounding': 'up'}, row
        assert row['display'] == f'0 of {days} (prorated: {days} of 21 days)'
    assert len(checked['findings']) == 900
    # Matching sessions exist, so the real blocker is the cause; the proration stays in its metadata.
    assert all(finding['cause']['code'] == 'unfilled' and finding['cause']['sessions_found'] > 0
               and finding['cause']['proration']['amount'] == expected[finding['person_id']]
               for finding in checked['findings'])


@pytest.mark.cpu_budget
def test_department_working_day_proration_cpu_budget(department_proration):
    *_, measured = department_proration
    assert measured['cpu_seconds'] < python_budget(20), measured
