"""Offered overtime is a dated union of available windows, shared by reports and the solver."""
import copy
from datetime import date, timedelta
import time
import uuid

import pytest
from ortools.sat.python import cp_model

from packages.domain.cp_sat_goals import add_balance_goals
from packages.domain.conditions import active_rules, with_tags
from packages.domain.fairness import goal_report, offered_amounts, strengths_of
from packages.domain.generation import allocation_settings
from packages.domain.validation import person_in_scope
from packages.domain.time import aware
from tests.test_performance_budget import python_budget


def ident(name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'overtime-offered:' + name))


def snapshot(people=3, first='2026-10-04', last='2026-10-11'):
    ids = [ident('person-' + str(n)) for n in range(people)]
    return {'organisation': {'timezone': 'Asia/Riyadh'},
            'workspace': {'department_id': ident('department'), 'starts_on': first, 'ends_on': last},
            'people': {pid: {'id': pid, 'display_name': 'Synthetic person ' + str(n)} for n, pid in enumerate(ids)},
            'types': {ident('type'): {'department_id': ident('department')}},
            'memberships': [{'person_id': pid, 'department_id': ident('department'), 'starts_on': '2020-01-01',
                             'ends_on': None, 'category_id': ident('rank')} for pid in ids],
            'availability': [], 'rotations': [], 'policies': [], 'scheduling_facts': [], 'shifts': [], 'focus_ids': []}


def offer(data, pid, name, first='2026-10-04', last='2026-10-11', start='08:00', end='12:00', **line):
    fact = {'id': ident(name), 'kind': 'overtime_offer', 'status': 'active', 'on': True,
            'department_id': data['workspace']['department_id'], 'person_id': pid, 'starts_on': first, 'ends_on': last,
            'data': {'catalogue_id': 'overtime_offers', 'catalogue_version': 1,
                     'line': {'id': ident('line-' + name), 'numbers': {'apply': {'value': None, 'level': 'medium'}},
                              'start_time': start, 'end_time': end, **line}}}
    data['scheduling_facts'].append(fact)
    return fact


def balance(data, metric='overtime_duties', period='rota', name='goal'):
    policy = {'id': ident(name), 'name': 'Overtime fair share', 'kind': 'balance_goal', 'status': 'active',
              'active': True, 'revision': 1, 'department_id': data['workspace']['department_id'],
              'starts_on': '2020-01-01', 'ends_on': None,
              'definition': {'severity': 'advisory', 'strength': 'medium', 'scope': {},
                             'rule': {'kind': 'balance_goal', 'metric': metric, 'aim': 'equal_share',
                                      'share_by': 'offered', 'period': period}}}
    data['policies'].append(policy)
    return policy


def away(data, pid, first, last, **values):
    data['availability'].append({'person_id': pid, 'kind': 'recorded_leave',
                                 'start': first + '+03:00', 'end': last + '+03:00', **values})


def test_planner_and_request_fact_windows_union_once_and_clip_to_membership():
    data = snapshot()
    a, b, c = data['people']
    data['memberships'][0].update(starts_on='2026-10-05', ends_on='2026-10-06')
    offer(data, a, 'planner', start='08:00', end='12:00')
    request = offer(data, a, 'request', start='10:00', end='16:00')
    request['request_id'] = ident('request-source')
    duplicate = copy.deepcopy(request)
    duplicate['id'] = ident('duplicate-request')
    data['scheduling_facts'].append(duplicate)
    offer(data, b, 'proposed')['status'] = 'proposed'
    offer(data, b, 'disabled')['on'] = False
    offer(data, c, 'other-department')['department_id'] = ident('other-department')
    assert offered_amounts(data, date(2026, 10, 4), date(2026, 10, 11)) == {a: 8, b: 0, c: 0}
    assert offered_amounts(data, date(2026, 10, 5), date(2026, 10, 5), [a]) == {a: 0}


def test_leave_and_unavailability_subtract_their_exact_union_and_external_rotations():
    data = snapshot(1)
    [a] = data['people']
    offer(data, a, 'two-days', first='2026-10-05', last='2026-10-07', start='08:00', end='16:00')
    away(data, a, '2026-10-05T09:00:00', '2026-10-05T11:00:00')
    away(data, a, '2026-10-05T10:00:00', '2026-10-05T12:00:00', kind='unavailable')
    away(data, a, '2026-10-05T12:00:00', '2026-10-05T16:00:00', retracted=True)
    away(data, a, '2026-10-05T14:00:00', '2026-10-05T15:00:00', counts_as_unavailable=False)
    data['rotations'].append({'person_id': a, 'starts_on': '2026-10-06', 'ends_on': '2026-10-07',
                              'department_available': False})
    assert offered_amounts(data, date(2026, 10, 4), date(2026, 10, 11)) == {a: 5}


def test_independent_whenever_free_line_keeps_its_window_alongside_a_different_offer():
    data = snapshot()
    a, b, c = data['people']
    data['policies'].append({'id': ident('whenever-free'), 'name': 'Whenever free', 'kind': 'allocation_overtime',
        'department_id': data['workspace']['department_id'], 'status': 'active', 'active': True,
        'starts_on': '2020-01-01', 'ends_on': None,
        'definition': {'severity': 'advisory', 'strength': 'medium',
                       'scope': {'weekdays': [0, 2], 'start_time': '08:00', 'end_time': '12:00'},
                       'rule': {'kind': 'allocation_overtime'}}})
    offer(data, a, 'limited', first='2026-10-05', last='2026-10-06', end='10:00')
    offer(data, c, 'expired', first='2026-09-01', last='2026-10-01')
    away(data, b, '2026-10-05T09:00:00', '2026-10-05T10:00:00', kind='unavailable')
    assert offered_amounts(data, date(2026, 10, 4), date(2026, 10, 11)) == {a: 8, b: 7, c: 8}
    away(data, a, '2026-10-05T08:00:00', '2026-10-05T10:00:00')
    assert offered_amounts(data, date(2026, 10, 4), date(2026, 10, 11), [a]) == {a: 6}


def resolved_offers(*, same_where=False):
    data = snapshot(1, last='2026-10-18')
    [pid] = data['people']
    fm = {'match': 'all', 'conditions': [{'field': 'service_ids', 'op': 'any', 'values': [ident('fm')]}]}
    ucc = {'match': 'all', 'conditions': [{'field': 'service_ids', 'op': 'any', 'values': [ident('ucc')]}]}
    general = {'id': ident('department-line'), 'where': fm, 'weekdays': [0],
               'start_time': '08:00', 'end_time': '12:00',
               'numbers': {'apply': {'value': None, 'level': 'medium'}}}
    own = {**general, 'id': ident('person-line'),
           'who': {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [pid]}]},
           'where': fm if same_where else ucc, 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
           'weekdays': [0] if same_where else [1]}
    source = {'id': ident('catalogue-offers'), 'name': 'Synthetic overtime offers',
        'kind': 'catalogue', 'status': 'active', 'active': True, 'revision': 1,
        'department_id': data['workspace']['department_id'], 'starts_on': '2020-01-01', 'ends_on': None,
        'definition': {'scope': {}, 'severity': 'advisory', 'strength': 'medium',
                       'rule': {'kind': 'catalogue', 'id': 'overtime_offers', 'settings': {}, 'lines': [general, own]}}}
    department = {**source, 'id': ident('department-offers'), 'definition': {**source['definition'],
                  'rule': {**source['definition']['rule'], 'lines': [general]}}}
    fact = offer(data, pid, 'bound-person', **{k: v for k, v in own.items() if k != 'id'})
    data['policies'] = [department]
    # Frozen lineage reunites the department line and the independently approved person fact.
    data['source_bindings'] = [{'source_order': 0, 'source_policy_id': source['id'], 'source_revision': 1,
        'source': source, 'source_kind': 'catalogue', 'source_catalogue_id': 'overtime_offers',
        'rule_shape': source['definition']['rule'], 'destinations': [
            {'kind': 'policy', 'id': department['id'], 'line_id': general['id'],
             'source_line_id': general['id'], 'source_line_index': 0},
            {'kind': 'fact', 'id': fact['id'], 'person_id': pid,
             'source_line_id': own['id'], 'source_line_index': 1}]}]
    return data, pid


def test_person_ucc_offer_and_department_fm_line_with_different_where_both_count():
    data, pid = resolved_offers()
    # Both department Mondays plus the person's UCC Tuesday, using the same policies as generation.
    assert offered_amounts(data, date(2026, 10, 4), date(2026, 10, 18)) == {pid: 12}
    scheduler = with_tags(copy.deepcopy(data))
    scheduler['policies'] = active_rules(scheduler['policies'], context=scheduler)
    for day, service in [('2026-10-05', 'fm'), ('2026-10-06', 'ucc'), ('2026-10-12', 'fm')]:
        shift = {'assignment_type_id': ident('type'), 'service_ids': [ident(service)],
                 'start': day + 'T08:00:00+03:00', 'end': day + 'T12:00:00+03:00', 'participants': []}
        assert allocation_settings(scheduler, shift, pid)[1]


def test_person_line_wins_same_where_only_while_its_dates_apply():
    data, pid = resolved_offers(same_where=True)
    scheduler = with_tags(copy.deepcopy(data))
    scheduler['policies'] = active_rules(scheduler['policies'], context=scheduler)
    general = next(p for p in scheduler['policies'] if p['template_line']['line'] == 0)
    own = next(p for p in scheduler['policies'] if p['template_line']['line'] == 1)
    start, end = aware('2026-10-05T08:00:00+03:00'), aware('2026-10-05T12:00:00+03:00')
    assert not person_in_scope(scheduler, general['definition']['scope'], pid, start, end)
    assert person_in_scope(scheduler, own['definition']['scope'], pid, start, end)
    # The person owns the first Monday; the department line resumes on the next Monday.
    assert offered_amounts(data, date(2026, 10, 4), date(2026, 10, 11)) == {pid: 4}
    assert offered_amounts(data, date(2026, 10, 4), date(2026, 10, 18)) == {pid: 8}


@pytest.mark.parametrize('retired', [False, True], ids=['active', 'retired'])
def test_volunteer_only_personal_eligibility_does_not_count_as_offered_hours(retired):
    data = snapshot(1)
    [pid] = data['people']
    fact = offer(data, pid, 'eligibility-only', settings={'allocation_overtime': {'volunteer_required': True}})
    if retired:
        fact.update(status='retired', approved_by=ident('approver'), retired_at='2026-10-06T12:00:00+03:00')
    assert offered_amounts(data, date(2026, 10, 4), date(2026, 10, 11)) == {pid: 0}


def test_whenever_free_requires_its_configured_caps():
    data = snapshot(1)
    [pid] = data['people']
    cap_id = ident('required-cap')
    policy = balance(data)
    policy['definition']['rule'] = {'kind': 'allocation_overtime', 'required_policy_ids': [cap_id]}
    assert offered_amounts(data, date(2026, 10, 4), date(2026, 10, 11)) == {pid: 0}
    cap = copy.deepcopy(policy)
    cap.update(id=cap_id)
    cap['definition'].update(severity='mandatory', rule={'kind': 'block_quota', 'comparison': 'maximum'})
    data['policies'].append(cap)
    assert offered_amounts(data, date(2026, 10, 4), date(2026, 10, 11)) == {pid: 7 * 24}


def test_offers_use_fact_and_line_validity_dates_in_each_historical_period():
    data = snapshot(2, first='2026-10-11', last='2026-10-18')
    a, b = data['people']
    offer(data, a, 'a-past', first='2026-10-04', last='2026-10-11', end='12:00')
    offer(data, b, 'b-past', first='2026-10-04', last='2026-10-11', end='10:00')
    offer(data, a, 'a-current', first='2026-10-11', last='2026-10-18', end='10:00')
    offer(data, b, 'b-current', first='2026-10-11', last='2026-10-18', end='12:00')
    assert offered_amounts(data, date(2026, 10, 4), date(2026, 10, 11)) == {a: 28, b: 14}
    assert offered_amounts(data, date(2026, 10, 11), date(2026, 10, 18)) == {a: 14, b: 28}
    goal = balance(data, period='month')
    data['fairness_history'] = {goal['id']: {'people': {
        a: {'value': 3, 'available': 7, 'offered': 28}, b: {'value': 1, 'available': 7, 'offered': 14}}}}
    rows = {r['person_id']: r for r in goal_report(data, goal, planned_values=({a: 1, b: 3}, {}))['people']}
    assert {pid: row['overtime_offered_hours'] for pid, row in rows.items()} == {a: 42, b: 42}
    assert rows[a]['expected'] == rows[b]['expected'] == 4
    # Line dates narrow a broader fact; they must also be honored when querying a past period.
    data['scheduling_facts'][0]['data']['line'].update(starts_on='2026-10-05', ends_on='2026-10-06')
    assert offered_amounts(data, date(2026, 10, 4), date(2026, 10, 11))[a] == 4


def test_approved_replacement_preserves_predecessor_hours_until_its_effective_date_and_pending_does_not_replace():
    data = snapshot(2, first='2026-10-01', last='2026-10-15')
    a, b = data['people']
    previous = offer(data, a, 'approved-previous', first='2026-10-01', last='2026-11-01')
    previous.update(status='retired', approved_by=ident('reviewer'))
    replacement = offer(data, a, 'approved-replacement', first='2026-10-08', last='2026-11-01', end='10:00')
    replacement.update(replaces_id=previous['id'], approved_by=ident('reviewer'))
    current = offer(data, b, 'still-current', first='2026-10-01', last='2026-11-01')
    current['approved_by'] = ident('reviewer')
    proposed = offer(data, b, 'pending-replacement', first='2026-10-08', last='2026-11-01', end='10:00')
    proposed.update(status='proposed', replaces_id=current['id'])
    assert offered_amounts(data, date(2026, 10, 1), date(2026, 10, 8)) == {a: 28, b: 28}
    assert offered_amounts(data, date(2026, 10, 8), date(2026, 10, 15)) == {a: 14, b: 28}
    assert offered_amounts(data, date(2026, 10, 1), date(2026, 10, 15)) == {a: 42, b: 56}


def test_manual_retirement_cuts_off_an_approved_offer_at_the_recorded_instant():
    data = snapshot(2, first='2026-10-01', last='2026-10-15')
    a, b = data['people']
    previous = offer(data, a, 'manually-retired', first='2026-10-01', last='2026-11-01')
    previous.update(status='retired', approved_by=ident('reviewer'), retired_at='2026-10-08T10:30:00+03:00')
    never_approved = offer(data, b, 'unapproved-retired', first='2026-10-01', last='2026-11-01')
    never_approved.update(status='retired', retired_at='2026-10-08T10:30:00+03:00')
    assert offered_amounts(data, date(2026, 10, 1), date(2026, 10, 15)) == {a: 30.5, b: 0}
    assert offered_amounts(data, date(2026, 10, 8), date(2026, 10, 15)) == {a: 2.5, b: 0}
    assert offered_amounts(data, date(2026, 10, 9), date(2026, 10, 15)) == {a: 0, b: 0}


@pytest.mark.parametrize('metric', ['overtime_duties', 'overtime_hours'])
def test_goal_shares_are_pro_rata_to_offered_hours_and_zero_offer_has_no_penalty(metric):
    data = snapshot()
    a, b, c = data['people']
    offer(data, a, 'small', end='10:00')
    offer(data, b, 'large', end='14:00')
    goal = balance(data, metric)
    rows = {r['person_id']: r for r in goal_report(data, goal, planned_values=({a: 3, b: 9}, {}))['people']}
    assert rows[a]['overtime_offered_hours'] == 14 and rows[b]['overtime_offered_hours'] == 42
    assert (rows[a]['expected'], rows[b]['expected'], rows[c]['expected']) == (3, 9, 0)
    assert rows[c]['cost'] == 0 and rows[c]['status'] == 'within'
    no_offer_actual = goal_report(data, goal, planned_values=({c: 100}, {}))
    [zero] = [r for r in no_offer_actual['people'] if r['person_id'] == c]
    assert (zero['expected'], zero['cost'], zero['status']) == (0, 0, 'within')
    data['scheduling_facts'] = []
    assert all((r['expected'], r['cost'], r['status']) == (0, 0, 'within')
               for r in goal_report(data, goal, planned_values=({a: 100}, {}))['people'])


def goal_model(data, sessions, candidates=2):
    """Compile just the affected scheduling objective, with independently constrained coverage."""
    model = cp_model.CpModel()
    people = list(data['people'])
    choices, participants = {}, {}
    first = date.fromisoformat(data['workspace']['starts_on'])
    days = (date.fromisoformat(data['workspace']['ends_on']) - first).days
    for n in range(sessions):
        on = first + timedelta(days=n % days)
        shift = {'id': ident('session-' + str(n)), 'cancelled': False, 'assignment_type_id': ident('type'),
                 'start': f'{on}T08:00:00+03:00', 'end': f'{on}T12:00:00+03:00',
                 'type_snapshot': {'definition': {'requirements': [{'minimum': 1}]}},
                 'service_ids': [], 'participants': []}
        data['shifts'].append(shift)
        for offset in range(candidates):
            pid = people[(n + offset) % len(people)]
            key = shift['id'], pid
            choices[key] = model.new_bool_var('attends')
            participants[key] = {'person_id': pid, 'start': shift['start'], 'end': shift['end'], 'overtime': True}
        model.add(sum(choices[shift['id'], people[(n + offset) % len(people)]] for offset in range(candidates)) == 1)
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    terms, worst = add_balance_goals(model, data, choices, participants, choices, {}, set(data['focus_ids']), strengths_of(data))
    assert not model.validate(), model.validate()
    return model, choices, terms, worst


@pytest.mark.parametrize('metric', ['overtime_duties', 'overtime_hours'])
def test_cp_sat_uses_offered_shares_for_both_overtime_metrics(metric):
    data = snapshot(2, last='2026-10-05')
    a, b = data['people']
    offer(data, a, 'small-solver', end='08:15')
    offer(data, b, 'large-solver', end='08:45')
    balance(data, metric)
    model, choices, terms, _ = goal_model(data, 8)
    model.minimize(sum(terms))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5
    solver.parameters.num_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    assigned = {pid: sum(solver.value(x) for (_, who), x in choices.items() if who == pid) for pid in (a, b)}
    assert assigned == {a: 2, b: 6}


def test_cp_sat_adds_no_fairness_penalty_when_everyone_has_zero_offered_hours():
    data = snapshot(2)
    balance(data)
    _, _, terms, worst = goal_model(data, 8)
    assert terms == [] and worst == 0


@pytest.fixture(scope='module')
def department_offered_fairness():
    data = snapshot(150, first='2026-10-01', last='2027-01-01')
    for n, pid in enumerate(data['people']):
        offer(data, pid, 'scale-' + str(n), first='2026-10-01', last='2027-01-01', weekdays=[0, 2, 4])
        offer(data, pid, 'overlap-' + str(n), first='2026-10-01', last='2027-01-01', start='10:00', end='14:00', weekdays=[0, 2, 4])
    goal = balance(data, period='quarter')
    began = time.process_time()
    amounts = offered_amounts(data, date(2026, 10, 1), date(2027, 1, 1))
    report = goal_report(data, goal, planned_values=({pid: 10 for pid in data['people']}, {}))
    elapsed = time.process_time() - began
    return amounts, report, elapsed


def test_offered_fairness_department_quarter_budget(department_offered_fairness, request):
    amounts, report, elapsed = department_offered_fairness
    request.node.user_properties.append(('offered_fairness_cpu_seconds', elapsed))
    expected = sum((date(2026, 10, 1) + timedelta(days=n)).weekday() in (0, 2, 4) for n in range(92)) * 6
    assert len(amounts) == len(report['people']) == 150
    assert set(amounts.values()) == {expected}
    assert all(r['expected'] == 10 and r['overtime_offered_hours'] == expected for r in report['people'])


@pytest.mark.cpu_budget
def test_offered_fairness_department_quarter_budget_cpu_budget(department_offered_fairness):
    _, _, elapsed = department_offered_fairness
    assert elapsed < python_budget(3), elapsed
