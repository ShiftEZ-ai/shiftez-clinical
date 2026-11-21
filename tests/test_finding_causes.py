"""Constructed lower-bound causes preserve the shared evaluator's actual obligations."""
import copy
import uuid

import pytest

from packages.contracts.schemas import FindingCause, FindingFixHint, PolicyDefinition, SeriesDefinition, TypeDefinition
from packages.domain.validation import evaluate, finding_observations
from packages.domain import validation
from packages.domain.time import aware
from packages.domain.finding_causes import approved_leave_duty_ids


def uid(name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'finding-causes-' + name))


@pytest.fixture(scope='module')
def cause_snapshot(scale_harness):
    h = scale_harness()
    workspace = h.shift(h.workspace('Cause explanations'), people=[])['workspace']
    data = h.snapshot(workspace)
    data['policies'], data['availability'], data['rotations'], data['recurring_series'] = [], [], [], []
    data['quota_context'] = {'complete': True, 'start': '2026-10-01T00:00:00+03:00',
                             'end': '2026-11-01T00:00:00+03:00', 'publications': []}
    return data, h.seed['people'][0]


@pytest.fixture
def cause_data(cause_snapshot):
    return copy.deepcopy(cause_snapshot)


def policy(data, rule, scope=None, name='quota'):
    row = {'id': uid(name), 'name': 'Constructed ' + name, 'department_id': data['workspace']['department_id'],
           'revision': 1, 'status': 'active', 'active': True, 'kind': rule['kind'],
           'starts_on': '2020-01-01', 'ends_on': None,
           'definition': PolicyDefinition.model_validate({'rule': rule, 'scope': scope or {},
                                                        'severity': 'mandatory'}).model_dump(mode='json')}
    data['policies'].append(row)
    return row


def quota(data, pid, *, scope=None, **changes):
    return policy(data, {'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
                        'metric': 'duties', 'comparison': 'minimum', 'amount': 1, **changes},
                  {'person_id': pid, **(scope or {})})


def finding(data, code='QUOTA_BELOW_MINIMUM'):
    checked = evaluate(data)
    result = next(f for f in finding_observations(checked) if f['code'] == code)
    FindingCause.model_validate(result['cause'])
    for hint in result['fix_hints']:
        FindingFixHint.model_validate(hint)
    return result


def type_definition(data, **changes):
    shift = data['shifts'][0]
    definition = TypeDefinition.model_validate({**shift['type_snapshot']['definition'], **changes}).model_dump(mode='json')
    data['types'][shift['assignment_type_id']]['definition'] = copy.deepcopy(definition)
    shift['type_snapshot']['definition'] = definition


def away(data, pid, first, last):
    data['rotations'].append({'id': uid('away'), 'person_id': pid,
        'department_id': data['workspace']['department_id'], 'cohort_id': uid('cohort'),
        'rotation_type_id': uid('rotation-type'), 'service_id': data['shifts'][0]['service_ids'][0],
        'starts_on': first, 'ends_on': last, 'department_available': False})


def test_approved_leave_duty_classification_only_marks_overlapping_active_duties():
    person = 'person-1'
    data = {'availability': [{'person_id': person, 'kind': 'approved_leave',
                              'start': '2026-10-04T09:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00'}],
            'shifts': [
                {'id': 'overlap', 'participants': [{'person_id': person,
                    'start': '2026-10-04T10:00:00+03:00', 'end': '2026-10-04T11:00:00+03:00'}]},
                {'id': 'outside', 'participants': [{'person_id': person,
                    'start': '2026-10-04T13:00:00+03:00', 'end': '2026-10-04T14:00:00+03:00'}]},
                {'id': 'closed', 'closed': True, 'participants': [{'person_id': person,
                    'start': '2026-10-04T10:00:00+03:00', 'end': '2026-10-04T11:00:00+03:00'}]},
            ]}
    assert approved_leave_duty_ids(data, person) == {'overlap'}


def test_empty_rota_explains_minimum_without_erasing_person_obligation(cause_data):
    data, pid = cause_data
    shift = data['shifts'][0]
    scope = {'assignment_type_id': shift['assignment_type_id'], 'service_id': shift['service_ids'][0],
             'location_id': shift['location_id'], 'weekdays': [6], 'start_time': '08:00', 'end_time': '12:00'}
    rule = quota(data, pid, scope=scope)
    data['shifts'], data['focus_ids'] = [], []
    frozen = copy.deepcopy(data)
    result = finding(data)
    assert data == frozen
    assert result['severity'] == 'error' and result['person_id'] == pid and result['rule_id'] == rule['id']
    assert result['cause'] == {'code': 'no_candidate_sessions', 'sessions_found': 0, 'eligible_people': 0,
        'places_available': 0, 'days_in_window': 7, 'days_available': 7, 'required': 1,
        'possible': 0, 'actual': 0, 'basis': 'known_records_not_solver_proof'}
    hint = result['fix_hints'][0]
    assert hint['action'] == 'add_sessions'
    assert {key: hint['prefill'][key] for key in ('starts_on', 'ends_on', 'assignment_type_ids', 'service_ids',
        'location_id', 'weekdays', 'start_time', 'end_time')} == {
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'assignment_type_ids': [scope['assignment_type_id']],
        'service_ids': [scope['service_id']], 'location_id': scope['location_id'],
        'weekdays': [6], 'start_time': '08:00', 'end_time': '12:00'}


@pytest.mark.parametrize('difference', ['kind', 'clinic', 'location', 'weekday', 'time', 'cancelled', 'closed'])
def test_candidate_sessions_follow_exact_scope_and_active_state(cause_data, difference):
    data, pid = cause_data
    shift = data['shifts'][0]
    scope = {'service_id': shift['service_ids'][0], 'location_id': shift['location_id'],
             'weekdays': [6], 'start_time': '08:00', 'end_time': '12:00'}
    quota(data, pid, scope=scope, assignment_type_ids=[shift['assignment_type_id']])
    if difference == 'kind':
        other = copy.deepcopy(data['types'][shift['assignment_type_id']])
        other['id'] = uid('other-type')
        data['types'][other['id']] = other
        shift.update(assignment_type_id=other['id'], type_snapshot=other)
    elif difference == 'clinic':
        shift['service_ids'] = [sid for sid in data['services'] if sid != scope['service_id']][:1]
    elif difference == 'location':
        shift['location_id'] = next(lid for lid in data['locations'] if lid != scope['location_id'])
    elif difference == 'weekday':
        shift.update(start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')
    elif difference == 'time':
        shift.update(start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00')
    else:
        shift[difference] = True
    result = finding(data)
    assert result['cause']['code'] == 'no_candidate_sessions'
    assert result['cause']['sessions_found'] == 0


def test_sessions_without_clinic_eligibility_offer_eligibility_fix(cause_data):
    data, pid = cause_data
    quota(data, pid)
    data['eligibilities'] = [r for r in data['eligibilities'] if r['person_id'] != pid]
    result = finding(data)
    assert (result['cause']['code'], result['cause']['sessions_found'], result['cause']['eligible_people']) == (
        'no_eligible_people', 1, 0)
    assert [h['action'] for h in result['fix_hints']] == ['open_eligibility']
    assert result['fix_hints'][0]['prefill']['person_id'] == pid


@pytest.mark.parametrize('blocker', ['leave', 'away'])
def test_leave_and_full_away_rotation_prorate_commitment_to_zero(cause_data, blocker):
    data, pid = cause_data
    quota(data, pid)
    if blocker == 'leave':
        data['availability'] = [{'id': uid('leave'), 'person_id': pid, 'kind': 'approved_leave',
            'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-11T00:00:00+03:00'}]
    else:
        away(data, pid, '2026-10-04', '2026-10-11')
    checked = evaluate(data)
    [row] = checked['quota_report']['rows']
    assert (row['amount'], row['actual'], row['status']) == (0, 0, 'met')
    assert row['proration']['working_days'] == 0
    assert not any(f['code'] == 'QUOTA_BELOW_MINIMUM' for f in checked['findings'])


def test_partial_leave_keeps_unreachable_minimum_cause_and_privacy(cause_data):
    data, pid = cause_data
    quota(data, pid, amount=1)
    data['availability'] = [{'id': uid('partial-leave'), 'person_id': pid, 'kind': 'approved_leave',
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-08T00:00:00+03:00'}]

    result = finding(data)

    assert result['cause']['code'] == 'person_unavailable'
    assert result['cause']['sessions_found'] == 1 and result['cause']['eligible_people'] == 0
    assert result['cause']['possible'] == 0
    assert [h['action'] for h in result['fix_hints']] == ['open_leave']
    assert 'private_reason' not in str(result)


@pytest.mark.parametrize('boundary,days,amount', [('rota', 3, 2), ('rotation', 1, 1)])
def test_partial_window_reports_overlap_and_prorated_minimum(cause_data, boundary, days, amount):
    data, pid = cause_data
    quota(data, pid, amount=2)
    if boundary == 'rota':
        data['workspace']['ends_on'] = '2026-10-07'
    else:
        away(data, pid, '2026-10-05', '2026-10-11')
    result = finding(data)
    assert result['cause']['code'] == ('capacity_short' if boundary == 'rota' else 'unfilled')
    assert (result['cause']['days_in_window'], result['cause']['days_available']) == (7, days)
    assert result['cause']['required'] == amount and result['severity'] == 'error'
    assert result['cause']['proration']['amount'] == amount
    assert {h['action'] for h in result['fix_hints']} == ({'add_sessions'} if boundary == 'rota' else {'run_auto_schedule'})


def test_quota_capacity_counts_one_opportunity_per_person_per_session(cause_data):
    data, pid = cause_data
    quota(data, pid, amount=2)
    type_definition(data, requirements=[{'minimum': 0, 'maximum': 10}])
    result = finding(data)
    assert result['cause']['code'] == 'capacity_short'
    assert (result['cause']['sessions_found'], result['cause']['eligible_people'],
            result['cause']['places_available'], result['cause']['possible']) == (1, 1, 1, 1)
    assert [h['action'] for h in result['fix_hints']] == ['add_sessions']


def test_upper_quota_offers_assignment_review_instead_of_adding_sessions(cause_data):
    data, pid = cause_data
    quota(data, pid, comparison='maximum', amount=0)
    shift = data['shifts'][0]
    shift['participants'] = [{'id': uid('assigned'), 'person_id': pid, 'start': shift['start'],
        'end': shift['end'], 'participation': 'staff', 'overtime': False, 'team_lead': False}]
    result = finding(data, 'QUOTA_ABOVE_MAXIMUM')
    assert result['cause']['code'] == 'above_limit' and result['cause']['actual'] == 1
    assert [hint['action'] for hint in result['fix_hints']] == ['review_assignments']


def test_count_each_clinic_keeps_missing_sessions_separate_from_an_existing_clinic(cause_data):
    data, pid = cause_data
    clinics = list(data['services'])[:2]
    quota(data, pid, scope={'conditions': {'conditions': [
        {'field': 'service_ids', 'op': 'any', 'values': clinics, 'count': 'each'}]}})
    observed = [f for f in finding_observations(evaluate(data)) if f['code'] == 'QUOTA_BELOW_MINIMUM']
    assert len(observed) == 2
    by_clinic = {f['detail']['count_bucket'][-1][1]: f for f in observed}
    existing = data['shifts'][0]['service_ids'][0]
    assert by_clinic[existing]['cause']['code'] == 'unfilled'
    missing = next(clinic for clinic in clinics if clinic != existing)
    assert by_clinic[missing]['cause']['code'] == 'no_candidate_sessions'
    assert by_clinic[missing]['fix_hints'][0]['prefill']['service_ids'] == [missing]


@pytest.mark.parametrize('metric,expected', [('scheduled_hours', 4), ('reserved_hours', 4),
    ('session_units', 2), ('workload_credit', 8), ('training_credit', 6)])
def test_quota_opportunity_uses_canonical_metric_credit(cause_data, metric, expected):
    data, pid = cause_data
    type_definition(data, session_units=2, workload_credit_per_hour=2, training_credit_per_hour=1.5)
    quota(data, pid, metric=metric, amount=expected)
    result = finding(data)
    assert result['cause']['code'] == 'unfilled'
    assert result['cause']['possible'] == expected and result['detail']['actual'] == 0
    assert [h['action'] for h in result['fix_hints']] == ['run_auto_schedule']


def test_overlapping_hour_opportunities_are_not_counted_twice(cause_data):
    data, pid = cause_data
    quota(data, pid, metric='scheduled_hours', amount=5)
    second = copy.deepcopy(data['shifts'][0])
    second['id'] = uid('overlapping-session')
    data['shifts'].append(second)
    data['focus_ids'].append(second['id'])
    result = finding(data)
    assert result['cause']['sessions_found'] == 2
    assert result['cause']['possible'] == 4 and result['cause']['code'] == 'capacity_short'


@pytest.mark.parametrize('case,expected', [('unfilled', 'unfilled'), ('eligibility', 'no_eligible_people'),
                                         ('people_short', 'capacity_short')])
def test_coverage_causes_count_candidates_instead_of_only_assignments(cause_data, case, expected):
    data, _ = cause_data
    minimum = len(data['people']) + 1 if case == 'people_short' else 1
    type_definition(data, requirements=[{'minimum': minimum}])
    if case == 'eligibility':
        data['eligibilities'] = []
    result = finding(data, 'COVERAGE_GAP')
    assert result['cause']['code'] == expected
    assert result['cause']['sessions_found'] == 1 and result['cause']['required'] == minimum
    assert result['cause']['eligible_people'] == (0 if case == 'eligibility' else len(data['people']))
    assert result['cause']['places_available'] is None
    assert result['fix_hints'][0]['prefill']['shift_ids'] == [data['shifts'][0]['id']]


def test_scoped_coverage_keeps_its_person_selection(cause_data):
    data, pid = cause_data
    row = policy(data, {'kind': 'minimum_cover', 'minimum': 2}, {'person_id': pid}, 'cover')
    result = finding(data, 'SCOPED_COVERAGE_GAP')
    assert result['cause']['code'] == 'capacity_short' and result['cause']['eligible_people'] == 1
    assert result['fix_hints'][0]['prefill']['rule_id'] == row['id']


def test_nested_capability_shortfall_keeps_its_own_cause(cause_data):
    data, _ = cause_data
    capability = next(iter(data['capabilities']))
    type_definition(data, requirements=[{'minimum': 1, 'capabilities': [{'capability_id': capability, 'minimum': 1}]}])
    data['grants'] = []
    result = finding(data, 'CAPABILITY_GAP')
    assert result['cause']['code'] == 'no_eligible_people'
    assert result['cause']['eligible_people'] == 0 and result['cause']['required'] == 1


def test_qualification_cache_shares_only_matching_dated_authority(monkeypatch):
    data = {'organisation': {'timezone': 'UTC'},
        'people': {'person': {'active': True}, 'inactive': {'active': False}},
        'types': {'a': {'department_id': 'a'}, 'b': {'department_id': 'b'}},
        'categories': {'rank': {}},
        'memberships': [{'person_id': 'person', 'department_id': 'a', 'category_id': 'rank',
            'starts_on': '2026-10-04', 'ends_on': '2026-10-05'}],
        'eligibilities': [{'id': 'eligibility', 'person_id': 'person', 'service_id': 'clinic',
            'starts_on': '2026-10-04', 'ends_on': '2026-10-05'}]}
    cached = {**data, '_qualification_cache': {}}
    calls, original = [], validation.qualification_failures

    def counted(context, *args):
        if '_qualification_cache' in context:
            calls.append(1)
        return original(context, *args)

    monkeypatch.setattr(validation, 'qualification_failures', counted)
    cases = [('person', 'a', ['clinic'], [], 4, 4, True),
        ('person', 'b', ['clinic'], [], 4, 4, False),
        ('person', 'a', ['other'], [], 4, 4, False),
        ('person', 'a', ['clinic', 'other'], [], 4, 4, False),
        ('person', 'a', ['clinic'], ['other-rank'], 4, 4, False),
        ('person', 'a', ['clinic'], [], 5, 5, False),
        ('person', 'a', ['clinic'], [], 4, 5, False),
        ('inactive', 'a', ['clinic'], [], 4, 4, False)]
    for pid, typ, services, ranks, first, last, expected in cases:
        a, b = aware(f'2026-10-{first:02}T08:00:00Z'), aware(f'2026-10-{last:02}T12:00:00Z')
        for sid in ('original-session', 'another-session'):
            shift = {'id': sid, 'assignment_type_id': typ, 'service_ids': services}
            definition = {'allowed_category_ids': ranks}
            assert validation.qualified(cached, pid, shift, definition, a, b) is expected
            assert validation.qualified(data, pid, shift, definition, a, b) is expected
    assert len(calls) == len(cases), 'Equivalent sessions reuse qualification, including negative results'


def test_coverage_optimization_preserves_report_and_refreshes_after_edits(cause_data, monkeypatch):
    data, _ = cause_data
    type_definition(data, requirements=[{'minimum': 1}])
    data['_qualification_cache'] = {}
    shift = data['shifts'][0]
    restricted = {**copy.deepcopy(shift), 'id': uid('restricted-session'),
        'location_id': next(lid for lid in data['locations'] if lid != shift['location_id'])}
    data['shifts'].append(restricted)
    data['focus_ids'].append(restricted['id'])
    policy(data, {'kind': 'restriction', 'weekdays': [6], 'start_time': '08:00', 'end_time': '12:00'},
           {'location_id': restricted['location_id']}, 'location-restriction')
    original = validation.rule_coverage_people

    def uncached_people(context, shift, a, b, **kwargs):
        kwargs.pop('presence', None)
        return original(context, shift, a, b, **kwargs)

    for edit in ('original', 'remove_eligibility', 'restore_eligibility'):
        if edit == 'remove_eligibility':
            eligibilities, data['eligibilities'] = data['eligibilities'], []
        elif edit == 'restore_eligibility':
            data['eligibilities'] = eligibilities
        before = copy.deepcopy(data)
        checked = evaluate(data)
        with monkeypatch.context() as patch:
            patch.setattr(validation, 'qualified', lambda context, *args:
                not validation.qualification_failures(context, *args))
            patch.setattr(validation, 'rule_coverage_people', uncached_people)
            assert evaluate(data) == checked
        assert data == before
        gaps = {f['shift_id']: f for f in checked['findings'] if f['code'] == 'COVERAGE_GAP'}
        assert gaps[shift['id']]['cause']['code'] == ('no_eligible_people' if edit == 'remove_eligibility' else 'unfilled')
        assert gaps[restricted['id']]['cause']['code'] == 'no_eligible_people'


def test_missing_sessions_hint_reuses_only_matching_recurring_templates(cause_data):
    data, pid = cause_data
    shift = data['shifts'][0]
    quota(data, pid, scope={'service_id': shift['service_ids'][0], 'location_id': shift['location_id']},
          assignment_type_ids=[shift['assignment_type_id']])
    definition = SeriesDefinition(anchor='2026-09-06', service_ids=shift['service_ids'],
        days=[{'weekday': 6, 'start_time': '08:00', 'end_time': '12:00'}]).model_dump(mode='json')
    base = {'department_id': data['workspace']['department_id'], 'assignment_type_id': shift['assignment_type_id'],
            'location_id': shift['location_id'], 'revision': 1, 'starts_on': '2026-09-01', 'ends_on': '2026-10-01',
            'definition': definition}
    data['recurring_series'] = [{**copy.deepcopy(base), 'id': uid('extend'), 'name': 'Existing matching pattern', 'active': True},
        {**copy.deepcopy(base), 'id': uid('reactivate'), 'name': 'Inactive matching pattern', 'active': False},
        {**copy.deepcopy(base), 'id': uid('unrelated'), 'name': 'Another clinic', 'active': True}]
    data['recurring_series'][-1]['definition']['service_ids'] = [sid for sid in data['services'] if sid not in shift['service_ids']][:1]
    data['shifts'], data['focus_ids'] = [], []
    matches = finding(data)['fix_hints'][0]['matching_series']
    assert {(row['id'], row['action']) for row in matches} == {(uid('extend'), 'extend'), (uid('reactivate'), 'reactivate')}


@pytest.mark.parametrize('blocker,expected', [(None,'unfilled'),('eligibility','no_eligible_people'),('leave','person_unavailable'),('sessions','no_candidate_sessions')])
def test_boundary_proration_is_metadata_not_a_rotation_repair(cause_data, blocker, expected):
    data, pid = cause_data
    quota(data, pid, amount=1)
    data['workspace']['ends_on'] = '2026-10-07'
    if blocker == 'eligibility':
        data['eligibilities'] = [r for r in data['eligibilities'] if r['person_id'] != pid]
    elif blocker == 'leave':
        data['availability'] = [{'id': uid('boundary-leave'), 'person_id': pid,
            'start': data['shifts'][0]['start'], 'end': data['shifts'][0]['end']}]
    elif blocker == 'sessions':
        data['shifts'], data['focus_ids'] = [], []
    result = finding(data)
    assert result['cause']['code'] == expected
    assert result['cause']['proration']['amount'] == result['cause']['required'] == 1
    assert result['cause']['days_available'] == 3
    assert not {'open_rotation','open_rota'} & {h['action'] for h in result['fix_hints']}
    assert result['cause']['basis'] == 'known_records_not_solver_proof'
