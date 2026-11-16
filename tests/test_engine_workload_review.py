"""Dated workload and exception regressions from the independent engine review."""
import copy

import pytest

from packages.contracts.schemas import PolicyDefinition
from packages.domain.cp_sat import generate
from packages.domain.rule_lines import scope_of
from packages.domain.validation import PERSON_ROWS, evaluate


PROGRAMME = '00000000-0000-4000-8000-000000000091'
POLICY = '00000000-0000-4000-8000-000000000092'


def policy(data, rule, selected=None, severity='mandatory', exceptions=False):
    return {'id': POLICY, 'revision': 1, 'name': 'Synthetic dated workload', 'active': True,
        'status': 'active', 'department_id': data['workspace']['department_id'],
        'starts_on': '2020-01-01', 'ends_on': None, 'kind': rule['kind'],
        'definition': PolicyDefinition.model_validate({'rule': rule, 'scope': selected or {},
            'severity': severity, 'exception_allowed': exceptions}).model_dump(mode='json')}


def one_person(data, pid):
    data['people'] = {pid: data['people'][pid]}
    for name in (*PERSON_ROWS, 'exceptions', 'wishes'):
        if name in data:
            data[name] = [row for row in data[name] if row['person_id'] == pid]
    return data


def programme_scope(nested):
    line = {'field': 'programme_ids', 'op': 'any', 'values': [PROGRAMME]}
    group = {'match': 'all', 'conditions': [line]}
    return scope_of({'who': {'match': 'all', 'conditions': [group]} if nested else group})


CASES = [
    ('duties', {'kind': 'max_duties', 'maximum': 1, 'window': 'week'},
        [('2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00', 0),
         ('2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', 0)], '2026-10-05'),
    ('hours', {'kind': 'max_hours', 'hours': 4, 'window': 'week'},
        [('2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00', 0),
         ('2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', 0)], '2026-10-05'),
    ('days', {'kind': 'max_consecutive_days', 'maximum': 1},
        [('2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00', 0),
         ('2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', 0)], '2026-10-05'),
    ('weekends', {'kind': 'max_consecutive_weekends', 'maximum': 1,
        'weekend_start': 4, 'weekend_days': 2},
        [('2026-10-02T08:00:00+03:00', '2026-10-02T12:00:00+03:00', 0),
         ('2026-10-09T08:00:00+03:00', '2026-10-09T12:00:00+03:00', 0)], '2026-10-09'),
    ('travel', {'kind': 'travel_buffer', 'minutes': 31},
        [('2026-10-04T23:00:00+03:00', '2026-10-04T23:30:00+03:00', 0),
         ('2026-10-05T00:00:00+03:00', '2026-10-05T00:30:00+03:00', 1)], '2026-10-05'),
]


@pytest.mark.parametrize('kind,rule,shifts,boundary', CASES, ids=[case[0] for case in CASES])
@pytest.mark.parametrize('membership', ['join', 'leave'])
@pytest.mark.parametrize('counted', [False, True])
def test_workload_counts_only_duties_inside_dated_scope(h, kind, rule, shifts, boundary, membership, counted):
    typ = h.configure('assignment_types', {'name': 'Synthetic dated workload', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']
    pid = h.seed['people'][0]
    workspace = h.workspace(start='2026-10-01', end='2026-10-15')
    for start, end, location in shifts:
        workspace = h.shift(workspace, start=start, end=end, typ=typ['id'], location=location,
            people=[pid])['workspace']
    data = one_person(h.snapshot(workspace), pid)
    data['organisation']['week_start'] = 6
    data['programmes'][PROGRAMME] = {'active': True, 'starts_on': '2020-01-01', 'ends_on': None}
    data['programme_memberships'] = [{'person_id': pid, 'programme_id': PROGRAMME,
        'starts_on': boundary if membership == 'join' else '2026-10-01',
        'ends_on': '2026-11-01' if membership == 'join' else boundary}]
    rule = copy.deepcopy(rule)
    if kind == 'travel':
        rule.update(from_location_id=h.seed['locations'][0], to_location_id=h.seed['locations'][1])
    for nested in (False, True):
        candidate = copy.deepcopy(data)
        scope = programme_scope(nested)
        if counted:
            scope.setdefault('conditions', {'match': 'all', 'conditions': []})['conditions'].append({'field': 'service_ids', 'op': 'any',
                'values': candidate['shifts'][0]['service_ids'], 'count': 'each'})
        candidate['policies'] = [policy(candidate, rule, scope)]
        findings = [f for f in evaluate(candidate)['findings'] if f.get('rule_id') == POLICY]
        assert findings == [], (nested, membership, findings)
        for shift in candidate['shifts']:
            shift['participants'] = []
        solved = generate(candidate, 3, workers=1, feasibility_only=True)
        assert solved['status'] == 'candidate_feasible', (nested, membership, solved)
        assert len(solved['additions']) == 2
        assert not [f for f in solved['validation']['findings'] if f.get('rule_id') == POLICY]


@pytest.mark.parametrize('membership', ['join', 'leave'])
@pytest.mark.parametrize('external_matches', [False, True], ids=['outside', 'inside'])
def test_external_workload_counts_known_dated_membership(h, membership, external_matches):
    pid = h.seed['people'][0]
    day = 5 if membership == 'join' else 4
    workspace = h.shift(h.workspace(), start=f'2026-10-{day:02}T08:00:00+03:00',
        end=f'2026-10-{day:02}T12:00:00+03:00', people=[pid])['workspace']
    data = one_person(h.snapshot(workspace), pid)
    data['organisation']['week_start'] = 6
    data['programmes'][PROGRAMME] = {'active': True, 'starts_on': '2020-01-01', 'ends_on': None}
    data['programme_memberships'] = [{'person_id': pid, 'programme_id': PROGRAMME,
        'starts_on': '2026-10-05' if membership == 'join' else '2026-10-01',
        'ends_on': '2026-11-01' if membership == 'join' else '2026-10-05'}]
    external_day = day if external_matches else 9 - day
    data['availability'].append({'id': 'synthetic-external-work', 'person_id': pid,
        'kind': 'external_commitment', 'start': f'2026-10-{external_day:02}T04:00:00+03:00',
        'end': f'2026-10-{external_day:02}T08:00:00+03:00'})
    for nested in (False, True):
        candidate = copy.deepcopy(data)
        candidate['policies'] = [policy(candidate, {'kind': 'max_hours', 'hours': 4, 'window': 'week'},
            programme_scope(nested))]
        findings = [f for f in evaluate(candidate)['findings'] if f.get('rule_id') == POLICY]
        assert [f['code'] for f in findings] == (['MAX_HOURS'] if external_matches else []), (nested, findings)
        if external_matches:
            assert findings[0]['detail']['actual'] == 8
            assert findings[0]['detail']['maximum'] == 4
        candidate['shifts'][0]['participants'] = []
        solved = generate(candidate, 3, workers=1, feasibility_only=True)
        assert solved['status'] == ('requirements_conflict' if external_matches else 'candidate_feasible'), solved


@pytest.mark.parametrize('exception', ['expired', 'declined', 'revision_stale', 'applicable', 'partially_applicable'])
def test_unrelated_exception_does_not_change_advisory_run_penalty(h, exception):
    typ = h.configure('assignment_types', {'name': 'Synthetic exception penalty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']
    workspace = h.workspace()
    for day in (4, 5, 6, 7):
        workspace = h.shift(workspace, typ=typ['id'], start=f'2026-10-{day:02}T08:00:00+03:00',
            end=f'2026-10-{day:02}T12:00:00+03:00', people=[])['workspace']
    pid = h.seed['people'][0]
    data = one_person(h.snapshot(workspace), pid)
    data['policies'] = [policy(data, {'kind': 'max_consecutive_days', 'maximum': 1},
        severity='advisory', exceptions=True)]
    before = generate(data, 3, workers=1)
    data['exceptions'].append({'id': 'synthetic-exception', 'status': 'declined' if exception == 'declined' else 'approved',
        'policy_id': POLICY, 'policy_revision': 0 if exception == 'revision_stale' else 1, 'person_id': pid,
        'starts_on': '2026-01-01' if exception == 'expired' else '2026-10-04',
        'ends_on': '2026-01-02' if exception == 'expired' else
            '2026-10-06' if exception == 'partially_applicable' else '2026-10-08'})
    after = generate(data, 3, workers=1)
    assert before['status'] == after['status'] == 'candidate_feasible', (before, after)
    assert len(before['additions']) == len(after['additions']) == 4
    costs = [1 - solved['objective_components']['rules']['value'] for solved in (before, after)]
    for solved in (before, after):
        assert solved['compiled_component_values']['rules'] == solved['objective_components']['rules']['value']
    warnings = [len([f for f in solved['validation']['findings'] if f['code'] == 'CONSECUTIVE_DAYS'])
        for solved in (before, after)]
    assert costs[0] > 0 and warnings[0] == 3
    if exception == 'applicable':
        assert costs[1] == 0 and warnings[1] == 0
        assert len([f for f in after['validation']['findings'] if f['code'] == 'RULE_EXCEPTION']) == 3
    elif exception == 'partially_applicable':
        assert costs[1] == costs[0] == 1 and warnings[1] == 2  # A remaining breach fully violates this rule instance.
        assert len([f for f in after['validation']['findings'] if f['code'] == 'RULE_EXCEPTION']) == 1
    else:
        assert costs[0] == costs[1], (exception, costs)
        assert warnings == [3, 3]
