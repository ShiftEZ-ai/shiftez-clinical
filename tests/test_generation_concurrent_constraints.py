"""Concurrent leadership must preserve the assignment index consumed by every later compiler."""
from copy import deepcopy

import pytest

from packages.domain.cp_sat import generate
from packages.domain.generation import citation_finding_key
from packages.domain.proposal_reconciliation import reconcile
from packages.domain.validation import evaluate
from tests.test_engine_workload_review import policy
from tests.test_generation_population import scope
from tests.test_governed_leadership import line


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('kept_leader', [False, True], ids=['empty-lead-group', 'occupied-lead-group'])
@pytest.mark.parametrize('rule,days,field', [
    ({'kind': 'max_duties', 'maximum': 1, 'window': 'rolling', 'window_days': 8,
      'group_by': 'weekday'}, (4, 11), 'maximum'),
    ({'kind': 'max_hours', 'hours': 4, 'window': 'week'}, (4, 5), 'hours'),
    ({'kind': 'max_consecutive_days', 'maximum': 1}, (4, 5), 'maximum'),
    ({'kind': 'max_consecutive_days', 'maximum': 1, 'overtime_extension_days': 1}, (4, 5), 'maximum'),
    ({'kind': 'max_consecutive_weekends', 'maximum': 1, 'weekend_start': 4,
      'weekend_days': 2}, (2, 9), 'maximum'),
    ({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-10-15',
      'metric': 'duties', 'comparison': 'maximum', 'amount': 1}, (4, 5), 'amount'),
])
def test_concurrent_leadership_retains_workload_and_quota_constraints(h, kept_leader, rule, days, field):
    person, leader = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'Synthetic required work', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    lead_type = h.configure('assignment_types', {'name': 'Synthetic shared leadership', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']['id']
    w = h.workspace(start='2026-10-01', end='2026-10-15')
    for day in days:
        w = h.shift(w, typ=typ, start=f'2026-10-{day:02}T08:00:00+03:00',
                    end=f'2026-10-{day:02}T12:00:00+03:00', people=[])['workspace']
    w = h.shift(w, typ=lead_type, start='2026-10-14T08:00:00+03:00', end='2026-10-14T12:00:00+03:00',
                people=[leader] if kept_leader else [])['workspace']
    data = h.snapshot(w)
    last = next(s for s in data['shifts'] if s['assignment_type_id'] == lead_type)
    last['locked'] = True
    for participant in last['participants']:
        participant['team_lead'] = True
    requirement = line(lead_type, [leader], minimum=0)
    requirement['settings']['team_lead_requirement']['count_scope'] = 'concurrent_duties'
    shared = policy(data, {'kind': 'catalogue', 'id': 'team_leads', 'lines': [requirement]})
    shared['id'] = '00000000-0000-4000-8000-000000000093'
    limit = policy(data, rule, {'assignment_type_id': typ})
    data['policies'] = [shared, limit]
    # This synthetic quota is introduced after snapshot loading; all its source duties are above.
    data['quota_context'] = {'complete': True, 'start': '2026-10-01T00:00:00+03:00',
                             'end': '2026-10-15T00:00:00+03:00'}
    data['generation_scope'] = scope('person_ids', person)
    blocked = generate(data, 10, workers=1)
    assert blocked['status'] == 'requirements_conflict', (blocked['status'], blocked.get('message'))
    assert not blocked['additions'] and not blocked['removals']

    # The feasible replacement still has an unmet advisory goal: it must translate and reconcile.
    limit['definition']['rule'][field] *= 2
    fair = policy(data, {'kind': 'balance_goal', 'metric': 'duties', 'aim': 'target', 'amount': 10,
                        'period': 'rota', 'tolerance_percent': 0, 'tolerance_units': 0},
                  {'person_id': person}, severity='advisory')
    fair['id'] = '00000000-0000-4000-8000-000000000094'
    data['policies'].append(fair)
    solved = generate(data, 10, workers=1)
    assert solved['status'] == 'candidate_feasible', (solved['status'], solved.get('validation'))
    assert len(solved['additions']) == 2
    advisory = [f for f in solved['validation']['findings'] if f['code'] == 'BALANCE_GOAL']
    assert advisory and all(f['severity'] == 'warning' and f['detail']['goal_id'] for f in advisory)
    applied = reconcile(data, deepcopy(data), solved, list(range(len(solved['additions']))), [])
    assert applied['status'] == 'ready'
    assert evaluate(applied['data'])['valid']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_protected_approved_leave_keeps_identity_through_final_check_and_apply(h):
    person, cover = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'Synthetic covered duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 3}]}})['record']['id']
    w = h.shift(h.workspace(), typ=typ, people=[person, cover])['workspace']
    w = h.shift(w, typ=typ, start='2026-10-05T08:00:00+03:00',
                end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    data['policies'] = []
    kept = next(s for s in data['shifts'] if s['participants'])
    kept['locked'] = True
    data['availability'].append({'id': '00000000-0000-4000-8000-000000000095',
                                 'person_id': person, 'kind': 'approved_leave',
                                 'start': kept['start'], 'end': kept['end']})
    plain = next(f for f in evaluate(data, explain_findings=False)['findings'] if f['code'] == 'UNAVAILABLE')
    decorated = next(f for f in evaluate(data)['findings'] if f['code'] == 'UNAVAILABLE')
    assert decorated['detail']['leave_shift_ids'] == [kept['id']]
    assert citation_finding_key(plain) == citation_finding_key(decorated)
    assert citation_finding_key(plain) != citation_finding_key({**decorated, 'end': '2026-10-05T12:00:00+03:00'})
    result = generate(data, 10, workers=1)
    assert result['status'] == 'candidate_with_gaps', result.get('message')
    assert result['additions'] and not result['removals']
    assert any(f['code'] == 'UNAVAILABLE' for f in result['issues'])
    applied = reconcile(data, deepcopy(data), result, list(range(len(result['additions']))), [])
    assert applied['status'] == 'ready'
    assert any(f['code'] == 'UNAVAILABLE' for f in applied['validation']['findings'])


def test_finding_identity_retains_mandatory_magnitudes():
    before = {'code': 'SCOPED_COVERAGE_GAP', 'shift_id': 'duty', 'detail': {'required': 2, 'available': 1}}
    assert citation_finding_key(before) != citation_finding_key({**before, 'detail': {'required': 2, 'available': 0}})
# Include this file in hosted supervision and team-lead qualification.
