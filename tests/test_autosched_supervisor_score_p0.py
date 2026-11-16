"""Supervisor qualification follows admitted choices without changing allocation opportunities."""
from collections import Counter
import copy
import time

from ortools.sat.python import cp_model
import pytest

from packages.domain import allocation_score
from packages.domain.conditions import active_rules
from packages.domain.cp_sat_score import SCORE_SCALE, ScoreModel
from tests.test_performance_budget import python_budget
from tests.test_weighted_preferences import _rule, _snapshot
from tests.test_schedule_score import shift


def supervisor_snapshot(case='present', *, balance='none', days=1):
    sessions = [shift(f's{day}', day, ['a', 'b']) for day in range(4, 4 + days)]
    rule = _rule('supervisor', {'kind': 'allocation_preference', 'priority': 10,
        'supervisor_ids': ['b'], 'balance_by': balance, 'balance_window': 'rota'}, scope={'person_id': 'a'})
    data = _snapshot(sessions, [rule])
    data['policies'] = [rule]
    data['capabilities'] = {'supervise': {'active': True, 'starts_on': '2020-01-01'}}
    data['grants'] = [{'person_id': 'b', 'capability_id': 'supervise', 'department_id': 'd',
                       'service_id': None, 'starts_on': '2020-01-01', 'ends_on': None}]
    for session in sessions:
        session['type_snapshot']['definition']['supervision'] = {
            'trainee_category_ids': [], 'capability_id': 'supervise', 'trainees_per_supervisor': 1}
        session['participants'][0]['participation'] = 'trainee'
        session['participants'][1]['participation'] = 'supervisor'
    first = sessions[0]
    pairs = {}
    if case.startswith('absent'):
        first['participants'] = first['participants'][:1]
        if case == 'absent-named':
            first['participants'][0]['supervisor_person_id'] = 'b'
        elif case == 'absent-paired':
            pairs[first['id'], 'a', 'b'] = 1
    elif case == 'late-start':
        first['participants'][1]['start'] = '2026-10-04T10:00:00+00:00'
    elif case == 'expired-grant':
        data['grants'][0]['ends_on'] = '2026-10-04'
    elif case == 'overnight-grant-gap':
        first.update(start='2026-10-04T20:00:00+00:00', end='2026-10-05T08:00:00+00:00')
        for person in first['participants']:
            person.update(start=first['start'], end=first['end'])
        data['grants'][0]['ends_on'] = '2026-10-05'
    elif case == 'conflicting-duty':
        other = copy.deepcopy(first)
        other.update(id='independent', participants=[copy.deepcopy(first['participants'][1])])
        data['shifts'].append(other)
    elif case == 'mentor':
        rule['definition']['rule'].update(supervisor_ids=[], supervisor_relationship='mentor')
        data['mentorships'] = [{'person_id': 'a', 'mentor_id': 'b', 'starts_on': '2020-01-01', 'ends_on': None}]
    data['policies'] = active_rules(data['policies'], context=data)
    return data, pairs


def score_items(data, pairs, *, symbolic=False, dense=False, zero=False):
    participants = {(session['id'], person['person_id']): person
                    for session in data['shifts'] for person in session['participants']}
    choices = dict.fromkeys(participants, 1)
    if zero:
        choices[data['shifts'][0]['id'], 'c'] = 0
    if dense:
        for session in data['shifts']:
            for person in data['people']:
                choices.setdefault((session['id'], person), 0)
    if symbolic:
        model = cp_model.CpModel()
        for pair, selected in list(choices.items()):
            if selected:
                choices[pair] = model.new_bool_var('selected')
                model.add(choices[pair] == selected)
        items = allocation_score.allocation_score_items(data, choices, participants, pairs, math=ScoreModel(model))
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        solver.parameters.max_time_in_seconds = 2
        assert solver.solve(model) == cp_model.OPTIMAL
        value = solver.value
    else:
        items = allocation_score.allocation_score_items(data, choices if dense or zero else None,
                                                       participants, pairs)
        value = lambda expression: expression
    return [(row['person_id'], row['label'], row['weight'], value(row['loss']), value(row['active']))
            for row in items]


@pytest.mark.parametrize('symbolic', [False, True], ids=['observed', 'symbolic'])
@pytest.mark.parametrize('case,loss', [
    ('present', 0), ('absent', SCORE_SCALE), ('absent-named', 0), ('absent-paired', 0),
    ('late-start', SCORE_SCALE), ('expired-grant', SCORE_SCALE),
    ('overnight-grant-gap', SCORE_SCALE), ('conflicting-duty', SCORE_SCALE), ('mentor', 0),
])
def test_supervisor_choice_index_preserves_dense_qualification_answers(case, loss, symbolic):
    data, pairs = supervisor_snapshot(case)
    # Explicit zero keys recreate the previous all-person qualification pool without changing any choices.
    expected = score_items(copy.deepcopy(data), pairs, symbolic=symbolic, dense=True)
    actual = score_items(data, pairs, symbolic=symbolic)
    assert actual == expected
    assert next(row[3] for row in actual if row[:2] == ('a', 'missed positive allocation priority')) == loss


@pytest.mark.parametrize('symbolic', [False, True], ids=['observed', 'symbolic'])
def test_plain_zero_choices_remain_safe_without_testing_symbolic_truth(symbolic):
    data, pairs = supervisor_snapshot()
    assert score_items(data, pairs, symbolic=symbolic, zero=True) == score_items(
        copy.deepcopy(data), pairs, symbolic=symbolic, dense=True)


@pytest.mark.parametrize('balance', ['supervisor', 'supervisor_group'])
@pytest.mark.parametrize('symbolic', [False, True], ids=['observed', 'symbolic'])
def test_supervisor_exposure_keeps_unselected_opportunities(balance, symbolic):
    data, pairs = supervisor_snapshot(balance=balance, days=5)
    for session in data['shifts'][:3]:
        session['participants'][0]['supervisor_person_id'] = 'b'
    for session in data['shifts'][-2:]:
        session['participants'] = session['participants'][:1]
    expected = score_items(copy.deepcopy(data), pairs, symbolic=symbolic, dense=True)
    actual = score_items(data, pairs, symbolic=symbolic)
    assert actual == expected
    assert next(row[3] for row in actual if row[1] == 'allocation exposure') == 3 * SCORE_SCALE // 11


@pytest.fixture(scope='module')
def supervisor_work():
    data, _ = supervisor_snapshot(days=24)
    data['people'].update({f'unselected-{n}': {'display_name': f'Synthetic {n}', 'active': True}
                           for n in range(140)})
    calls = Counter()
    original = allocation_score.allocation_supervisor_present

    def qualified(view, session, person, *interval):
        calls[session['id'], person['person_id']] += 1
        return original(view, session, person, *interval)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(allocation_score, 'allocation_supervisor_present', qualified)
        started = time.process_time()
        items = allocation_score.allocation_score_items(data)
        cpu = time.process_time() - started
    return items, calls, cpu


def test_supervisor_qualification_work_tracks_assignments_instead_of_department_size(supervisor_work):
    items, calls, _ = supervisor_work
    assert any(row['active'] for row in items)
    assert len(calls) == 48 and sum(calls.values()) == 48
    assert {person for _, person in calls} == {'a', 'b'}


@pytest.mark.cpu_budget
def test_sparse_supervisor_qualification_cpu_budget(supervisor_work):
    assert supervisor_work[2] < python_budget(1), supervisor_work[2]
