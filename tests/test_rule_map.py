"""The planner's rule map: which rules touch which sessions, people and days of a rota, and why (GET /rule-map),
plus the richer "Why?" rows for one session. Expected results are worked out by hand from the fixtures."""
import copy
import gc
import time
import uuid
from random import Random

import pytest

from packages.contracts.schemas import PolicyDefinition
from packages.domain.rule_map import rule_map
from packages.domain.time import aware
from .test_rule_conditions import line, tree
from .test_rule_templates import activate, propose

A0, A1 = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'   # Sunday, day 0
B0, B1 = '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00'   # Monday, day 1
C0, C1 = '2026-10-06T13:00:00+03:00', '2026-10-06T17:00:00+03:00'   # Tuesday afternoon, day 2
EVERY = [[0, 7]]


def U(name):
    """A stable id for a readable fixture name (rules may only reference ids)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def rule(data, rid, kind, scope=None, **definition):
    """A rule as a snapshot holds it (active, department-wide, from 2020), named `rid`."""
    return {'id': U(rid), 'name': rid, 'kind': kind['kind'], 'status': 'active', 'active': True, 'revision': 1,
            'department_id': data['workspace']['department_id'], 'starts_on': '2020-01-01', 'ends_on': None,
            'approved_by': 'approver-id', 'created_at': '2026-09-01T00:00:00+00:00',
            'definition': PolicyDefinition.model_validate({'rule': kind, 'scope': scope or {}, **definition}).model_dump(mode='json')}


HOURS = {'kind': 'max_hours', 'hours': 48}


def base(h):
    """Three sessions: a on day 0 (clinic 0, room 0), b on day 1 (clinic 1, room 1), c on day 2 afternoon (clinic 0, room 1)."""
    a, b, c = h.seed['people']
    w = h.workspace('Rule map', '2026-10-04', '2026-10-11')
    w = h.shift(w, A0, A1, people=[a])['workspace']
    w = h.shift(w, B0, B1, people=[b], service=1, location=1)['workspace']
    w = h.shift(w, C0, C1, people=[c], location=1)['workspace']
    return w, h.snapshot(w)


def entry(result, rid):
    return next(r for r in result['rules'] if rid in (r['id'], r['name']))


def mapped(data, *rules, originals=None):
    data = copy.deepcopy(data)
    data['policies'] = list(rules)
    return rule_map(data, originals)


# Who and what ----------------------------------------------------------------------------------------------------------

def test_person_and_everyone_rules(h):
    w, data = base(h)
    a, b, c = h.seed['people']
    result = mapped(data, rule(data, 'mine', HOURS, {'person_id': a}), rule(data, 'all', HOURS))
    assert result['days'][0] == '2026-10-04' and len(result['days']) == 7 and len(result['shifts']) == 3
    mine, everyone = entry(result, 'mine'), entry(result, 'all')
    assert (mine['sessions'], mine['people'], mine['days']) == ([0], {a: EVERY}, list(range(7)))
    assert mine['person_side'] and not mine['session_side'] and mine['named']['people'] == [a]
    assert mine['number'] == {'field': 'hours', 'value': 48, 'unit': 'hours', 'stricter': 'lower'}
    assert (mine['safety'], mine['adjustable'], mine['overridable'], mine['off']) == (False, True, True, False)
    assert everyone['sessions'] == [0, 1, 2] and everyone['people'] == {a: EVERY, b: EVERY, c: EVERY}
    assert not everyone['person_side'] and not everyone['session_side'] and everyone['named']['people'] == []


def test_clinic_place_duty_type_and_clock_rules_select_sessions(h):
    w, data = base(h)
    s = h.seed
    child = {**data['locations'][s['locations'][0]], 'id': 'inner-room', 'parent_id': s['locations'][0]}
    data['locations']['inner-room'] = child
    data['shifts'][1]['location_id'] = 'inner-room'           # b's session is in a room inside room 0
    data['types']['other-type'] = {**data['types'][s['type']], 'id': 'other-type'}
    data['shifts'][2]['assignment_type_id'] = 'other-type'
    result = mapped(data,
                    rule(data, 'clinic', HOURS, {'service_ids': [s['services'][0]]}),
                    rule(data, 'clinic-line', HOURS, {'conditions': tree(line('service_ids', s['services'][1]))}),
                    rule(data, 'place', HOURS, {'location_id': s['locations'][0]}),
                    rule(data, 'duty', HOURS, {'conditions': tree(line('assignment_type_ids', s['type']))}),
                    rule(data, 'not-duty', HOURS, {'conditions': tree(line('assignment_type_ids', s['type'], op='none'))}),
                    rule(data, 'monday', HOURS, {'weekdays': [0]}),
                    rule(data, 'afternoon', HOURS, {'start_time': '13:00', 'end_time': '17:00'}),
                    rule(data, 'cover', {'kind': 'minimum_cover', 'minimum': 1}, {'service_ids': [s['services'][1]]}),
                    rule(data, 'blocked', {'kind': 'restriction', 'weekdays': [6], 'start_time': '00:00', 'end_time': '24:00'}))
    clinic = entry(result, 'clinic')
    assert (clinic['sessions'], clinic['days'], clinic['named']['services'], clinic['session_side']) == ([0, 2], [0, 2], [s['services'][0]], True)
    assert entry(result, 'clinic-line')['sessions'] == [1] and entry(result, 'clinic-line')['named']['services'] == [s['services'][1]]
    place = entry(result, 'place')
    assert place['sessions'] == [0, 1] and set(place['named']['locations']) == {s['locations'][0], 'inner-room'}
    assert entry(result, 'duty')['sessions'] == [0, 1] and entry(result, 'duty')['named']['types'] == [s['type']]
    assert entry(result, 'not-duty')['sessions'] == [2] and entry(result, 'not-duty')['named']['types'] == []
    monday = entry(result, 'monday')
    assert (monday['sessions'], monday['days'], monday['named']['weekdays']) == ([1], [1], [0])
    assert entry(result, 'afternoon')['sessions'] == [2]
    assert entry(result, 'cover')['sessions'] == [1] and entry(result, 'cover')['safety']
    assert entry(result, 'blocked')['sessions'] == [0] and entry(result, 'blocked')['session_side']


@pytest.mark.parametrize('zone, first, last, times, expected_days', [
    ('Asia/Riyadh', '2026-10-04', '2026-10-11', [
        ('2026-10-03T23:30:00+03:00', '2026-10-04T00:30:00+03:00'),
        ('2026-10-04T00:00:00+03:00', '2026-10-04T01:00:00+03:00'),
        ('2026-10-05T00:00:00+03:00', '2026-10-05T01:00:00+03:00'),
        ('2026-10-10T23:30:00+03:00', '2026-10-11T00:30:00+03:00'),
        ('2026-10-11T00:00:00+03:00', '2026-10-11T01:00:00+03:00'),
    ], [0, 1, 6]),
    ('America/New_York', '2026-10-31', '2026-11-03', [
        ('2026-10-30T23:30:00-04:00', '2026-10-31T00:30:00-04:00'),
        ('2026-10-31T00:00:00-04:00', '2026-10-31T01:00:00-04:00'),
        ('2026-11-01T01:30:00-04:00', '2026-11-01T01:45:00-04:00'),
        ('2026-11-01T01:30:00-05:00', '2026-11-01T01:45:00-05:00'),
        ('2026-11-02T23:30:00-05:00', '2026-11-03T00:30:00-05:00'),
        ('2026-11-03T00:00:00-05:00', '2026-11-03T01:00:00-05:00'),
    ], [0, 1, 2]),
])
def test_session_days_use_local_start_and_exclude_outside_rota(h, zone, first, last, times, expected_days):
    _, data = base(h)
    data['organisation']['timezone'] = zone
    data['workspace'].update(starts_on=first, ends_on=last)
    source = data['shifts'][0]
    data['shifts'] = [{**source, 'id': U(f'boundary-{i}'), 'start': start, 'end': end, 'participants': []}
                      for i, (start, end) in enumerate(times)]
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    result = mapped(data, rule(data, 'cover', {'kind': 'minimum_cover', 'minimum': 1}))
    assert result['shifts'] == data['focus_ids']
    cover = entry(result, 'cover')
    # Focused sessions remain ordered, including those outside the rota; only their local start day enters its mask.
    assert cover['sessions'] == list(range(len(times)))
    assert cover['days'] == expected_days
    data['focus_ids'] = [data['shifts'][0]['id'], data['shifts'][-1]['id']]
    outside = mapped(data, rule(data, 'cover', {'kind': 'minimum_cover', 'minimum': 1}))
    assert outside['shifts'] == data['focus_ids']
    assert (entry(outside, 'cover')['sessions'], entry(outside, 'cover')['days']) == ([0, 1], [])


def test_person_conditions_follow_dates(h):
    """Rank, skill, programme, rotation type, cohort and tag, each read on the days the person holds it."""
    w, data = base(h)
    a, b, c = h.seed['people']
    dept = data['workspace']['department_id']
    R2, S2, P, RT, C = (U(x) for x in ('R2', 'S2', 'P', 'RT', 'C'))
    data['categories'][R2] = {'id': R2, 'active': True, 'starts_on': '2020-01-01'}
    old = next(r for r in data['memberships'] if r['person_id'] == b)
    old['ends_on'] = '2026-10-06'                                   # b is promoted on day 2
    data['memberships'].append({**old, 'id': 'promoted', 'category_id': R2, 'starts_on': '2026-10-06', 'ends_on': None})
    data['capabilities'][S2] = {'id': S2, 'active': True, 'starts_on': '2020-01-01'}
    data['grants'].append({'id': 'g', 'person_id': c, 'capability_id': S2, 'department_id': dept, 'service_id': None,
                           'starts_on': '2026-10-06', 'ends_on': None, 'active': True, 'revoked': False})
    data['programmes'][P] = {'id': P, 'active': True, 'starts_on': '2020-01-01', 'ends_on': None}
    data['programme_memberships'].append({'id': 'pm', 'person_id': a, 'programme_id': P, 'starts_on': '2026-10-04', 'ends_on': '2026-10-06'})
    data['rotation_types'][RT] = {'id': RT, 'active': True, 'starts_on': '2020-01-01'}
    data['cohorts'][C] = {'id': C, 'active': True, 'starts_on': '2026-01-01', 'ends_on': '2027-01-01'}
    data['rotations'].append({'id': 'rot', 'person_id': b, 'rotation_type_id': RT, 'cohort_id': C, 'department_available': True,
                              'starts_on': '2026-10-07', 'ends_on': '2026-10-09', 'active': True})
    data['people'][c]['tags'] = ['Night']
    result = mapped(data,
                    rule(data, 'rank', HOURS, {'conditions': tree(line('category_ids', R2))}),
                    rule(data, 'rank-flat', HOURS, {'category_id': R2}),
                    rule(data, 'skill', HOURS, {'conditions': tree(line('capability_ids', S2))}),
                    rule(data, 'programme', HOURS, {'conditions': tree(line('programme_ids', P))}),
                    rule(data, 'programme-flat', HOURS, {'programme_id': P}),
                    rule(data, 'rotation', HOURS, {'conditions': tree(line('rotation_type_ids', RT))}),
                    rule(data, 'cohort', HOURS, {'conditions': tree(line('cohort_ids', C))}),
                    rule(data, 'tag', HOURS, {'tags': ['night']}))
    rank = entry(result, 'rank')
    # b is R2 from day 2 only: their day-1 session is not checked by it.
    assert (rank['people'], rank['sessions'], rank['days']) == ({b: [[2, 7]]}, [], [2, 3, 4, 5, 6])
    assert entry(result, 'rank-flat')['people'] == {b: [[2, 7]]}
    assert (entry(result, 'skill')['people'], entry(result, 'skill')['sessions']) == ({c: [[2, 7]]}, [2])
    assert entry(result, 'programme')['people'] == {a: [[0, 2]]} and entry(result, 'programme')['sessions'] == [0]
    assert entry(result, 'programme-flat')['people'] == {a: [[0, 2]]}
    assert entry(result, 'rotation')['people'] == {b: [[3, 5]]}
    assert entry(result, 'cohort')['people'] == {b: [[3, 5]]}
    assert entry(result, 'tag')['people'] == {c: EVERY} and entry(result, 'tag')['sessions'] == [2]
    assert all(entry(result, r)['person_side'] and not entry(result, r)['session_side'] for r in ('rank', 'skill', 'tag'))


def test_skills_granted_for_some_clinics_are_exact_per_session(h):
    """A skill held at one clinic only: the rule holds for the person on that day (their days), but not on their session
    at another clinic; `not_here` says so, as the "Why?" panel (policy_applies) does."""
    w, data = base(h)
    a, b, c = h.seed['people']
    clinic0, clinic1 = h.seed['services']
    dept, S = data['workspace']['department_id'], U('S')
    data['capabilities'][S] = {'id': S, 'active': True, 'starts_on': '2020-01-01'}
    grant = {'capability_id': S, 'department_id': dept, 'starts_on': '2020-01-01', 'ends_on': None, 'active': True, 'revoked': False}
    data['grants'] += [{**grant, 'id': 'ga', 'person_id': a, 'service_id': clinic1}, {**grant, 'id': 'gb', 'person_id': b, 'service_id': None}]
    s0, s1 = data['shifts'][0], data['shifts'][1]
    s1['participants'].append({**s1['participants'][0], 'id': 'pa', 'person_id': a})       # a also works clinic 1 on day 1
    result = mapped(data, rule(data, 'skill', HOURS, {'conditions': tree(line('capability_ids', S))}),
                    rule(data, 'skill-at-clinic0', {'kind': 'minimum_cover', 'minimum': 1}, {'service_ids': [clinic0], 'conditions': tree(line('capability_ids', S))}),
                    rule(data, 'plain-skill', HOURS, {'conditions': tree(line('capability_ids', U('S-none')))}))
    people = entry(result, 'skill')
    assert people['people'] == {a: EVERY, b: EVERY} and people['sessions'] == [1] and people['not_here'] == {a: [0]}
    cover = entry(result, 'skill-at-clinic0')
    assert cover['sessions'] == [0, 2] and cover['not_here'] == {a: [0]}
    assert entry(result, 'plain-skill')['not_here'] == {}   # no clinic-only grant of that skill: nothing to ask per session
    assert s0['participants'][0]['person_id'] == a


def test_leave_buffer_restrictions_touch_only_sessions_near_the_persons_leave(h):
    """Days within the buffer of each person's own leave, and only the sessions someone works within theirs."""
    w, data = base(h)
    a, b, c = h.seed['people']
    data['availability'].append({'id': 'leave', 'person_id': b, 'kind': 'approved_leave',
                                 'start': '2026-10-06T00:00:00+03:00', 'end': '2026-10-07T00:00:00+03:00'})   # day 2
    every = list(range(7))
    result = mapped(data, rule(data, 'near-leave', {'kind': 'restriction', 'weekdays': every, 'start_time': '00:00', 'end_time': '24:00', 'leave_buffer_days': 1}),
                    rule(data, 'near-leave-mornings', {'kind': 'restriction', 'weekdays': every, 'start_time': '08:00', 'end_time': '12:00', 'leave_buffer_days': 1}),
                    rule(data, 'far', {'kind': 'restriction', 'weekdays': every, 'start_time': '00:00', 'end_time': '24:00', 'leave_buffer_days': 1},
                         {'person_ids': [a]}))
    near = entry(result, 'near-leave')
    assert (near['people'], near['sessions'], near['days']) == ({b: [[1, 4]]}, [1], [1, 2, 3])   # b works day 1, next to their leave
    mornings = entry(result, 'near-leave-mornings')
    assert mornings['session_side'] and mornings['sessions'] == [1] and mornings['people'] == {b: [[1, 4]]}
    assert (entry(result, 'far')['people'], entry(result, 'far')['sessions']) == ({}, [])   # a has no leave


# Hierarchy and marks ---------------------------------------------------------------------------------------------------

def test_template_lines_overrides_and_exceptions(h):
    w, data = base(h)
    a, b, c = h.seed['people']
    template = rule(data, 'Weekly hours', HOURS, per_person=[{'person_id': a, 'amount': 40},
                                                             {'person_id': b, 'amount': 44, 'starts_on': '2026-10-06'}])
    general = rule(data, 'General', {'kind': 'max_duties', 'maximum': 5}, exception_allowed=True)
    narrow = rule(data, 'Narrow', {'kind': 'max_duties', 'maximum': 3}, {'person_ids': [c]}, overrides=[U('General')])
    data['exceptions'] = [
        {'id': 'x1', 'policy_id': U('General'), 'policy_revision': 1, 'person_id': b, 'starts_on': '2026-10-05', 'ends_on': '2026-10-07',
         'status': 'approved', 'approved_by': 'approver-id', 'created_at': '2026-09-02T00:00:00+00:00'},
        {'id': 'x2', 'policy_id': U('General'), 'policy_revision': 1, 'person_id': a, 'starts_on': '2026-10-05', 'ends_on': '2026-10-07',
         'status': 'proposed', 'approved_by': None}]
    result = mapped(data, template, general, narrow)
    t = entry(result, 'Weekly hours')
    assert t['people'] == {a: EVERY, b: EVERY, c: EVERY} and t['sessions'] == [0, 1, 2] and len(t['lines']) == 2
    assert t['named']['people'] == [a, b]
    lines = [m for m in result['markers'] if m['kind'] == 'template_line']
    assert [(m['people'], m['days'], m['text']) for m in lines] == [
        ([a], list(range(7)), 'Own number in “Weekly hours”: 40 hours instead of 48 hours'),
        ([b], [2, 3, 4, 5, 6], 'Own number in “Weekly hours”: 44 hours instead of 48 hours')]
    assert lines[0]['by'] == 'approver-id'
    g, n = entry(result, 'General'), entry(result, 'Narrow')
    assert g['people'] == {a: EVERY, b: EVERY} and g['replaced'] == {c: EVERY} and g['overridden_by'] == [U('Narrow')]
    assert g['sessions'] == [0, 1] and n['sessions'] == [2] and n['overrides'] == [U('General')]
    override = next(m for m in result['markers'] if m['kind'] == 'override')
    assert override['text'] == '“Narrow” replaces “General”: 3 duties instead of 5 duties'
    assert (override['people'], override['days'], override['rule_id']) == ([c], None, U('Narrow'))
    exceptions = [m for m in result['markers'] if m['kind'] == 'exception']
    assert [(m['people'], m['days'], m['by']) for m in exceptions] == [([b], [1, 2], 'approver-id')], 'approved ones only'
    assert exceptions[0]['text'] == 'Exception to “General”: 2026-10-05 to 2026-10-06'


def test_transitive_override_dates_remain_in_person_day_masks(h):
    _, data = base(h)
    a, b, c = h.seed['people']
    general = rule(data, 'General', {'kind': 'max_duties', 'maximum': 5})
    middle = rule(data, 'Middle', {'kind': 'max_duties', 'maximum': 4}, {'person_id': a}, overrides=[general['id']])
    middle.update(starts_on='2026-10-05', ends_on='2026-10-09')
    inner = rule(data, 'Inner', {'kind': 'max_duties', 'maximum': 3}, {'person_id': a}, overrides=[middle['id']])
    inner.update(starts_on='2026-10-06', ends_on='2026-10-08')
    result = mapped(data, general, middle, inner)
    assert entry(result, 'General')['people'] == {a: [[0, 1], [2, 4], [5, 7]], b: EVERY, c: EVERY}
    assert entry(result, 'Middle')['people'] == {a: [[1, 2], [4, 5]]}
    assert entry(result, 'Inner')['people'] == {a: [[2, 4]]}


def test_target_override_marker(h):
    w, data = base(h)
    a = h.seed['people'][0]
    quota = rule(data, 'Target', {'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties',
                                  'comparison': 'target', 'amount': 4, 'prorate_department_days': True}, severity='advisory')
    data['workspace']['target_overrides'] = [{'policy_id': U('Target'), 'person_id': a, 'amount': 2, 'reason': 'Part time',
                                              'policy_revision': 1, 'by': 'coordinator-id', 'at': '2026-09-03T00:00:00+00:00'}]
    result = mapped(data, quota)
    assert entry(result, 'Target')['number'] == {'field': 'amount', 'value': 4, 'unit': 'duties', 'stricter': None}
    assert entry(result, 'Target')['block'] == ['2026-10-01', '2026-11-01']
    [m] = result['markers']
    assert (m['kind'], m['people'], m['text'], m['by']) == ('target_override', [a], 'This rota’s target in “Target”: 2 duties', 'coordinator-id')


# The endpoint ------------------------------------------------------------------------------------------------------------

def url(w):
    return f"/api/workspaces/{w['id']}/rule-map"


def test_rota_adjustments_are_mapped_with_who_changed_them(h):
    workload = h.policy({'kind': 'max_hours', 'hours': 12, 'window_days': 7})
    w, _ = base(h)
    w = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': workload['id'], 'mode': 'set', 'amount': 8, 'reason': 'Workload agreed for this rota'},
                  w['id'], w['revision'])['workspace']
    stored = h.detail(w)['workspace']['rule_adjustments'][0]
    assert stored['by'] == h.seed['identities']['coordinator'] and stored['at']
    result = h.get('coordinator', url(w))
    r = entry(result, workload['id'])
    assert (r['number']['value'], r['department_value'], r['adjusted']['mode'], r['adjusted']['amount']) == (8, 12, 'set', 8)
    assert r['adjusted']['by'] == h.seed['identities']['coordinator']
    assert r['adjusted']['by_name'] == h.get('coordinator', '/api/me')['display_name']
    assert r['number']['stricter'] == 'lower' and not r['safety']
    [m] = result['markers']
    assert (m['kind'], m['text'], m['global'], m['by'], m['by_name']) == (
        'rota_adjustment', 'This rota: 8 hours instead of 12 hours', True,
        h.seed['identities']['coordinator'], h.get('coordinator', '/api/me')['display_name'])
    w = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': workload['id'], 'mode': 'off', 'reason': 'Paused'}, w['id'], w['revision'])['workspace']
    result = h.get('coordinator', url(w))
    r = entry(result, workload['id'])
    assert (r['off'], r['sessions'], r['people'], r['days']) == (True, [], {}, [])
    assert result['markers'][0]['text'] == 'Switched off for this rota'
    # Undo brings the number back; clearing still works on the stored entries.
    w = h.command('coordinator', 'workspace.undo', {'reason': 'Undo pause'}, w['id'], w['revision'])['workspace']
    assert h.detail(w)['workspace']['rule_adjustments'][0]['mode'] == 'set'
    h.command('coordinator', 'workspace.rule_adjust', {'policy_id': workload['id'], 'mode': 'clear', 'reason': 'Back'}, w['id'], w['revision'])


def test_rule_map_exposes_the_waiting_replacement_and_catalogue_proposal(h):
    active = h.policy({'kind': 'max_hours', 'hours': 12, 'window_days': 7})
    replacement = h.command('coordinator', 'policy.create', {
        'name': 'Hours replacement', 'owner': 'Test policy owner', 'source': 'Waiting replacement',
        'starts_on': '2020-01-01',
        'definition': {'rule': {'kind': 'max_hours', 'hours': 10, 'window_days': 7}, 'scope': {},
                       'severity': 'mandatory', 'replaces_id': active['id']},
    })['record']
    w, _ = base(h)
    result = h.get('coordinator', url(w))
    proposal = entry(result, active['id'])['proposal']
    assert proposal['policy_id'] == replacement['id']
    assert proposal['by_name'] == h.get('coordinator', '/api/me')['display_name']
    assert proposal['at'] and proposal['self_approval_allowed'] is False

    from .test_rule_catalogue_api import approve as approve_catalogue, line, propose as propose_catalogue
    from .test_rule_catalogue_v2_api import captured_snapshot
    catalogue = approve_catalogue(h, propose_catalogue(h, 'duties_per_week', [line(numbers={'max': {'value': 6}})])['record'])
    assert catalogue['id'] not in {policy['id'] for policy in captured_snapshot(h, w)['policies']}
    w = h.refresh_draft_baseline(w)
    w = h.shift(w)['workspace']
    w = h.command('coordinator', 'workspace.rule_adjust', {
        'policy_id': catalogue['id'], 'line_id': catalogue['definition']['rule']['lines'][0]['id'],
        'number': 'max', 'mode': 'set', 'amount': 4, 'reason': 'Short rota',
    }, w['id'], w['revision'])['workspace']
    waiting = propose_catalogue(h, 'duties_per_week', [line(numbers={'max': {'value': 5}})],
                               policy_id=catalogue['id'], revision=catalogue['revision'])['record']
    result = h.get('coordinator', url(w))
    catalogue_proposal = entry(result, catalogue['id'])['proposal']
    assert catalogue_proposal['policy_id'] == waiting['id']
    assert entry(result, active['id'])['proposal']['policy_id'] == replacement['id']


def test_rule_map_has_null_proposal_when_no_rule_change_is_waiting(h):
    workload = h.policy({'kind': 'max_hours', 'hours': 12, 'window_days': 7})
    w, _ = base(h)
    assert entry(h.get('coordinator', url(w)), workload['id'])['proposal'] is None


def test_rule_map_omits_lines_from_retired_catalogue_destination(h):
    _, data = base(h)
    from .test_rule_lines import line as catalogue_line, rule as catalogue_rule

    department_id = data['workspace']['department_id']
    active = catalogue_rule('hours_per_week', catalogue_line(id='display-active', numbers={'max': {'value': 8}}),
                            id=U('display-active-policy'), department_id=department_id)
    retired = catalogue_rule('hours_per_week', catalogue_line(id='display-retired', numbers={'max': {'value': 12}}),
                             id=U('display-retired-policy'), department_id=department_id)
    retired.update(status='retired', active=False)
    data.update(policies=[active, retired], _catalogue_v2_materialized=True,
                source_bindings=[{'source_policy_id': U('display-source'), 'source_catalogue_id': 'hours_per_week',
                                  'destinations': [{'kind': 'policy', 'id': retired['id']}]}])

    displayed = entry(rule_map(data), 'hours_per_week')

    assert [line['id'] for line in displayed['lines']] == ['display-active']


def test_rule_map_keeps_legacy_source_bound_only_to_retired_destination(h):
    _, data = base(h)
    from .test_rule_lines import line as catalogue_line, rule as catalogue_rule

    department_id = data['workspace']['department_id']
    legacy = rule(data, 'legacy-bound', HOURS)
    retired = catalogue_rule('workload_limit', catalogue_line(id='legacy-retired', numbers={'max': {'value': 5}}),
                             id=U('legacy-retired-policy'), department_id=department_id)
    retired.update(status='retired', active=False)
    data.update(policies=[legacy, retired], _catalogue_v2_materialized=True,
                source_bindings=[{'source_policy_id': legacy['id'], 'source_catalogue_id': 'hours_per_week',
                                  'destinations': [{'kind': 'policy', 'id': retired['id']}]}])

    reached = entry(rule_map(data), legacy['id'])

    assert reached['sessions'] == [0, 1, 2] and reached['people']


def test_rule_map_preserves_canonical_output_without_loading_fairness_history(h, monkeypatch):
    from apps.api import main
    from apps.api.services import fairness

    goal = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share', 'period': 'month'},
                    severity='advisory')
    previous = h.workspace('Earlier fairness history', '2026-10-01', '2026-10-04')
    previous = h.shift(previous, '2026-10-01T08:00:00+03:00', '2026-10-01T12:00:00+03:00',
                       people=[h.seed['people'][0]])['workspace']
    h.publish(previous)
    workspace, data = base(h)
    assert data['fairness_history'][goal['id']]['people'], 'The full snapshot has real publication history'
    original = main.domain_snapshot

    def full_snapshot(*args, **kwargs):
        return original(*args, **{**kwargs, 'include_fairness': True})

    with monkeypatch.context() as canonical:
        canonical.setattr(main, 'domain_snapshot', full_snapshot)
        expected = h.get('coordinator', url(workspace))

    def unexpected_history(*args, **kwargs):
        pytest.fail('Rule-map scopes and stored findings do not need fairness history')

    monkeypatch.setattr(fairness, 'history', unexpected_history)
    actual = h.get('coordinator', url(workspace))
    assert {key: value for key, value in actual.items() if key != 'elapsed_ms'} == {
        key: value for key, value in expected.items() if key != 'elapsed_ms'}


def test_status_etag_and_roles(h):
    a = h.seed['people'][0]
    broken = h.policy({'kind': 'max_hours', 'hours': 2, 'window': 'day'}, {'person_id': a})
    advisory = h.policy({'kind': 'max_hours', 'hours': 2, 'window': 'day'}, {'person_id': a}, severity='advisory')
    fine = h.policy({'kind': 'max_hours', 'hours': 100})
    w, _ = base(h)   # creating and editing a rota checks it
    first = h.client('coordinator').get(url(w))
    assert first.status_code == 200 and first.headers['etag']
    result = first.json()
    assert (result['checked'], result['fresh']) == (True, True) and isinstance(result['elapsed_ms'], int)
    assert entry(result, broken['id'])['status'] == {'state': 'breach', 'errors': 1, 'warnings': 0}
    assert entry(result, advisory['id'])['status']['state'] == 'warning'
    assert entry(result, fine['id'])['status'] == {'state': 'met', 'errors': 0, 'warnings': 0}
    # The days the check found short: a's four hours on day 0 (Sunday) break the two-hour day limit.
    (day, errors, warnings, message), = entry(result, broken['id'])['short_days']
    assert (day, errors, warnings) == (0, 1, 0) and message
    assert [d[:3] for d in entry(result, advisory['id'])['short_days']] == [[0, 0, 1]] and entry(result, fine['id'])['short_days'] == []
    again = h.client('coordinator').get(url(w), headers={'if-none-match': first.headers['etag']})
    assert again.status_code == 304 and again.headers['etag'] == first.headers['etag']
    w = h.shift(w, '2026-10-07T08:00:00+03:00', '2026-10-07T12:00:00+03:00', people=[a])['workspace']
    assert h.client('coordinator').get(url(w), headers={'if-none-match': first.headers['etag']}).status_code == 200
    assert entry(h.get('coordinator', url(w)), broken['id'])['sessions'] == [0, 3]
    h.policy({'kind': 'max_hours', 'hours': 90})   # the rules changed since the last check
    stale = h.get('coordinator', url(w))
    assert (stale['checked'], stale['fresh']) == (True, False) and entry(stale, broken['id'])['status']['state'] == 'unchecked'
    for role in ('approver', 'auditor'):
        h.get(role, url(w))
    for role in ('staff', 'credentialer', 'operator'):
        h.get(role, url(w), status=403)


def test_why_panel_rows(h):
    a, b, c = h.seed['people']
    general = h.policy({'kind': 'max_hours', 'hours': 48})
    narrow = activate(h, propose(h, {'kind': 'max_hours', 'hours': 40}, {'person_ids': [a]}, name='Narrow', overrides=[general['id']]))
    template = activate(h, propose(h, {'kind': 'max_duties', 'maximum': 5}, name='Duties', exception_allowed=True, per_person=[{'person_id': a, 'amount': 3}]))
    exc = h.command('coordinator', 'exception.create', {'policy_id': template['id'], 'person_id': a, 'starts_on': '2026-10-01',
                                                        'ends_on': '2026-10-10', 'reason': 'Agreed extra duties'})['record']
    h.command('approver', 'exception.approve', {'reason': 'Independent review'}, exc['id'], exc['revision'])
    w, _ = base(h)
    sid = h.detail(w)['shifts'][0]['id']
    [person] = h.get('coordinator', f"/api/workspaces/{w['id']}/shifts/{sid}/rules")['people']
    rows = {r['id']: r for r in person['rules']}
    assert [r['status'] for r in person['rules']][-1] == 'replaced'
    assert rows[general['id']]['status'] == 'replaced' and rows[general['id']]['replaced_by'] == [{'id': narrow['id'], 'name': 'Narrow'}]
    assert rows[narrow['id']]['number']['value'] == 40 and rows[narrow['id']]['status'] == 'passes'
    duties = rows[template['id']]
    assert duties['number']['value'] == 3 and duties['own_line']['person_id'] == a and duties['own_line']['amount'] == 3
    assert duties['exception'] == {'starts_on': '2026-10-01', 'ends_on': '2026-10-10', 'by': 'Approver · Demo'}
    assert (duties['adjusted'], duties['safety'], duties['exception_allowed'], duties['adjustable']) == (False, False, True, True)


# Size ------------------------------------------------------------------------------------------------------------------

def big_department(seed=11):
    """A month: 142 people, ~534 sessions, ~974 active rules of every sort, built as a snapshot directly."""
    rng = Random(seed)
    dept, zone = 'dept', 'Asia/Riyadh'
    ids = lambda name, n: [U(f'{name}{i}') for i in range(n)]
    people = [U(f'p{i:03}') for i in range(142)]
    ranks, skills, tags = ids('R', 5), ids('S', 6), ['night', 'senior', 'weekend']
    services, places, types = ids('svc', 12), ids('room', 10), ids('type', 3)
    programmes, rotation_types, cohorts = ids('P', 3), ids('RT', 3), ids('C', 3)
    data = {'organisation': {'timezone': zone}, 'focus_ids': [], 'shifts': [], 'exceptions': [], 'eligibilities': [], 'availability': [],
            'workspace': {'id': 'w', 'department_id': dept, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'rule_adjustments': [], 'target_overrides': []},
            'types': {t: {'id': t, 'department_id': dept} for t in types},
            'services': {s: {'id': s, 'department_id': dept, 'active': True} for s in services},
            'locations': {U('wing'): {'id': U('wing'), 'department_id': dept, 'parent_id': None},
                          **{r: {'id': r, 'department_id': dept, 'parent_id': U('wing') if i < 5 else None} for i, r in enumerate(places)}},
            'categories': {r: {'id': r, 'active': True, 'starts_on': '2020-01-01'} for r in ranks},
            'capabilities': {s: {'id': s, 'active': True, 'starts_on': '2020-01-01'} for s in skills},
            'programmes': {p: {'id': p, 'active': True, 'starts_on': '2020-01-01'} for p in programmes},
            'rotation_types': {r: {'id': r, 'active': True, 'starts_on': '2020-01-01'} for r in rotation_types},
            'cohorts': {c: {'id': c, 'active': True, 'starts_on': '2026-01-01', 'ends_on': '2027-01-01'} for c in cohorts},
            'people': {p: {'id': p, 'active': True, 'tags': rng.sample(tags, rng.randint(0, 2))} for p in people},
            'memberships': [], 'grants': [], 'programme_memberships': [], 'rotations': [], 'policies': []}
    for n, p in enumerate(people):
        if n % 7 == 0:  # promoted mid-month
            data['memberships'] += [{'id': f'm{n}a', 'person_id': p, 'department_id': dept, 'category_id': ranks[0], 'starts_on': '2020-01-01', 'ends_on': '2026-10-15'},
                                    {'id': f'm{n}b', 'person_id': p, 'department_id': dept, 'category_id': ranks[1], 'starts_on': '2026-10-15'}]
        else:
            data['memberships'].append({'id': f'm{n}', 'person_id': p, 'department_id': dept, 'category_id': rng.choice(ranks), 'starts_on': '2020-01-01'})
        for k, s in enumerate(rng.sample(skills, 2)):   # every fourth person holds their first skill at one clinic only
            data['grants'].append({'id': f'g{n}{s}', 'person_id': p, 'department_id': dept, 'capability_id': s,
                                   'service_id': services[n % 12] if n % 4 == 0 and k == 0 else None,
                                   'starts_on': rng.choice(['2020-01-01', '2026-10-10']), 'revoked': False})
        if n % 6 == 0:   # a week of leave for one in six
            data['availability'].append({'id': f'l{n}', 'person_id': p, 'kind': 'approved_leave',
                                         'start': f'2026-10-{1 + n % 20:02}T00:00:00+03:00', 'end': f'2026-10-{8 + n % 20:02}T00:00:00+03:00'})
        if n % 3 == 0:
            data['programme_memberships'].append({'id': f'pm{n}', 'person_id': p, 'programme_id': programmes[n % 9 // 3], 'starts_on': '2026-09-01', 'ends_on': '2026-10-20'})
        if n % 5 == 0:
            data['rotations'].append({'id': f'r{n}', 'person_id': p, 'rotation_type_id': rotation_types[n % 3], 'cohort_id': cohorts[n % 3],
                                      'department_available': True, 'starts_on': '2026-10-05', 'ends_on': '2026-10-25'})
    for day in range(1, 32):
        for k in range(17 + (day % 7 == 0)):
            typ = types[k % 3]
            hours = [('08:00', '12:00'), ('13:00', '17:00'), ('20:00', '23:00')][k % 3]
            start, end = (f'2026-10-{day:02}T{t}:00+03:00' for t in hours)
            sid = f's{day}-{k}'
            data['focus_ids'].append(sid)
            data['shifts'].append({'id': sid, 'workspace_id': 'w', 'start': start, 'end': end, 'cancelled': False, 'assignment_type_id': typ,
                                   'service_ids': [services[(day + k) % 12]], 'location_id': places[k % 10],
                                   'participants': [{'person_id': p, 'start': start, 'end': end} for p in rng.sample(people, rng.randint(2, 4))]})
    base = {'status': 'active', 'active': True, 'revision': 1, 'department_id': dept, 'starts_on': '2020-01-01', 'ends_on': None, 'approved_by': None}

    def add(rid, kind, scope=None, **definition):
        data['policies'].append({**base, 'id': U(rid), 'name': rid, 'kind': kind['kind'],
                                 'definition': PolicyDefinition.model_validate({'rule': kind, 'scope': scope or {}, **definition}).model_dump(mode='json')})
    quota = {'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties', 'comparison': 'maximum', 'amount': 20}
    for i in range(700):
        p = people[i % 142]
        add(f'own{i}', {'kind': 'max_hours', 'hours': 40 + i % 10} if i % 2 else quota, {'person_id': p} if i % 3 else {'person_ids': [p]})
    for i in range(5):
        add(f'template{i}', {'kind': 'max_duties', 'maximum': 6},
            per_person=[{'person_id': p, 'amount': 4} for p in rng.sample(people, 40)])
    fields = [('category_ids', ranks), ('capability_ids', skills), ('programme_ids', programmes),
              ('rotation_type_ids', rotation_types), ('cohort_ids', cohorts), ('tags', tags)]
    add('not-R0', {'kind': 'max_duties', 'maximum': 5}, {'conditions': tree(line('category_ids', ranks[0], op='none'))})
    for i in range(149):
        field, values = fields[i % len(fields)]
        add(f'cond{i}', {'kind': 'max_duties', 'maximum': 5 + i % 3}, {'conditions': tree(line(field, rng.choice(values), op='none' if i % 10 == 0 else 'any'))})
    for i in range(80):
        scope = [{'service_ids': [services[i % 12]]}, {'location_id': U('wing') if i % 8 == 1 else places[i % 10]},
                 {'conditions': tree(line('assignment_type_ids', types[i % 3]))}, {'weekdays': [i % 7], 'start_time': '08:00', 'end_time': '12:00'}][i % 4]
        add(f'session{i}', [{'kind': 'minimum_cover', 'minimum': 1}, HOURS, {'kind': 'restriction', 'weekdays': [5], 'start_time': '00:00', 'end_time': '24:00'}][i % 3], scope)
    for i in range(33):
        add(f'everyone{i}', {'kind': 'max_consecutive_days', 'maximum': 5 + i % 3})
    add('near-leave', {'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00', 'leave_buffer_days': 2})
    for i in range(5):
        add(f'narrow{i}', {'kind': 'max_consecutive_days', 'maximum': 4}, {'conditions': tree(line('category_ids', ranks[i]))}, overrides=[U(f'everyone{i}')])
    return data


def test_map_subview_caches_preserve_uncached_clinic_overnight_and_source_pool_answers(monkeypatch):
    import packages.domain.rule_map as mapping
    from .test_rule_lines import line as catalogue_line, person, rule as catalogue_rule
    from .test_rule_lines_inherit import n

    data = big_department()
    overnight = data['shifts'][0]
    overnight['end'] = '2026-10-02T12:00:00+03:00'
    for participant in overnight['participants']:
        participant['end'] = overnight['end']
    person_id = U('p000')
    if not any(p['person_id'] == person_id for p in overnight['participants']):
        overnight['participants'].append({'person_id': person_id, 'start': overnight['start'], 'end': overnight['end']})
    where = tree(line('service_ids', overnight['service_ids'][0]))
    catalogue = catalogue_rule('duties_per_week',
        catalogue_line(who=tree(line('category_ids', U('R0'))), where=where, numbers={'max': n(8, 'low')}),
        catalogue_line(who=person(person_id), where=where, numbers={'max': n(3, None)}),
        id=U('cached-scope-catalogue'), department_id=data['workspace']['department_id'])
    data['policies'].append(catalogue)
    original = mapping.policy_applies

    def uncached(view, *args):
        return original({key: value for key, value in view.items()
                         if key not in {'_policy_applies_cache', '_policy_applies_people'}}, *args)

    with monkeypatch.context() as canonical:
        canonical.setattr(mapping, 'policy_applies', uncached)
        expected = mapping.rule_map(data)

    contexts = {}

    def cached(view, *args):
        cache = view['_policy_applies_cache']
        _, pools = contexts.setdefault(id(cache), (cache, set()))
        pools.add(tuple(id(policy) for policy in view['policies']))
        return original(view, *args)

    monkeypatch.setattr(mapping, 'policy_applies', cached)
    actual = mapping.rule_map(data)
    assert actual == expected
    assert any(row['not_here'] for row in actual['rules'])  # Clinic-specific skill checks remain exact.
    assert entry(actual, 'everyone0')['overridden_by'] and entry(actual, catalogue['id'])['lines']
    assert len(contexts) > 1 and all(len(pools) == 1 for _, pools in contexts.values())


@pytest.fixture(scope='module')
def department_map_measurement():
    data = big_department()
    gc.collect()  # Exclude earlier fixtures' garbage; collection remains enabled during mapping.
    began, cpu = time.perf_counter(), time.process_time()
    result = rule_map(data)
    elapsed, cpu = time.perf_counter() - began, time.process_time() - cpu
    print(f'rule map: {elapsed:.2f} wall s / {cpu:.2f} CPU s for 974 rules, 142 people, {len(data["shifts"])} sessions')
    return data, result, elapsed, cpu


@pytest.mark.cpu_budget
def test_large_department_mapping_time(department_map_measurement):
    _, _, _, cpu = department_map_measurement
    # CPU retains the 1.5 s target without other workers' contention; coverage tracing is about seven times slower.
    import coverage
    budget = 1.5 * (7 if coverage.Coverage.current() else 1)
    assert cpu < budget, (cpu, budget)


@pytest.mark.wall_benchmark
def test_large_department_mapping_latency(department_map_measurement):
    import coverage
    _, _, elapsed, _ = department_map_measurement
    budget = 1.5 * (7 if coverage.Coverage.current() else 1)
    assert elapsed < 4 * budget, (elapsed, 4 * budget)


def test_large_department_is_fast_and_right(department_map_measurement):
    data, result, _, _ = department_map_measurement
    assert len({p['id'] for p in data['policies']}) == 974 and len(data['shifts']) == 17 * 31 + 4
    index = {sid: i for i, sid in enumerate(result['shifts'])}
    worked = lambda p: sorted(index[s['id']] for s in data['shifts'] if any(x['person_id'] == p for x in s['participants']))
    own = entry(result, 'own3')
    assert own['people'] == {U('p003'): [[0, 31]]} and own['sessions'] == worked(U('p003'))
    svc = entry(result, 'session0')
    assert svc['sessions'] == sorted(index[s['id']] for s in data['shifts'] if U('svc0') in s['service_ids'])
    wing = entry(result, 'session1')   # the wing: every room inside it
    assert wing['sessions'] == sorted(index[s['id']] for s in data['shifts'] if s['location_id'] in {U(f'room{i}') for i in range(5)})
    promoted = entry(result, 'not-R0')   # the promoted people join on 15 Oct
    assert promoted['people'][U('p007')] == [[14, 31]] and promoted['people'][U('p000')] == [[14, 31]]
    everyone = entry(result, 'everyone0')
    assert everyone['overridden_by'] == [U('narrow0')] and set(everyone['replaced']) == set(entry(result, 'narrow0')['people'])
    template = entry(result, 'template0')
    assert len(template['lines']) == 40 and all(p in template['people'] for p in (r['person_id'] for r in template['lines']))
    # Clinic-only skills: every (person, session) the map leaves out is one the validator's own test says no to, and the rest say yes.
    from packages.domain.conditions import active_rules
    from packages.domain.validation import index_person_rows, policy_applies
    view = {**data, '_person_rows': index_person_rows(data)}
    by_id = {p['id']: p for p in active_rules(data['policies'])}
    checked = 0
    for r in result['rules']:
        if not r['not_here'] and not any(c['field'] == 'capability_ids' for c in (by_id[r['id']]['definition']['scope'].get('conditions') or {}).get('conditions', [])):
            continue
        for i in r['sessions'][:40]:
            s = data['shifts'][i]
            for p in s['participants']:
                day = int(s['start'][8:10]) - 1
                if any(a <= day < b for a, b in r['people'].get(p['person_id'], [])):
                    exact = policy_applies(view, by_id[r['id']], p['person_id'], s, aware(p['start']), aware(p['end']))
                    assert exact == (i not in r['not_here'].get(p['person_id'], [])), (r['name'], i, p['person_id'])
                    checked += 1
    assert checked > 100 and sum(len(v) for r in result['rules'] for v in r['not_here'].values()) > 0
    # Leave buffer: only people with leave, only their days within two days of it, only sessions they work then.
    near = entry(result, 'near-leave')
    assert near['people'] and set(near['people']) <= {a['person_id'] for a in data['availability']}
    assert all(any(p['person_id'] in near['people'] for p in data['shifts'][i]['participants']) for i in near['sessions'])
    assert 0 < len(near['sessions']) < len(data['shifts']) // 2
