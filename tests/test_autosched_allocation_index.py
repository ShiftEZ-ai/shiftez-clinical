"""Session-only allocation indexing preserves opportunities and compiled arithmetic."""
from collections import Counter
import copy
import hashlib
import json
import time

from ortools.sat.python import cp_model
import pytest

from packages.contracts.schemas import GenerationOptions
from packages.domain import allocation_score, cp_sat
from packages.domain.cp_sat_score import ScoreModel, begin_score_program, finish_score_program
from packages.domain.solver_control import SolverControl, TimeLimit
from packages.domain.solver_resume import prepared_state
from tests.test_autosched_supervisor_score_p0 import supervisor_snapshot
from tests.test_performance_budget import python_budget
from tests.test_schedule_score import shift
from tests.test_schedule_score_scale import department
from tests.test_weighted_preferences import _rule, _snapshot


def allocation_snapshot(match, balance):
    data, _ = supervisor_snapshot(balance=balance, days=5)
    data['focus_ids'] = [row['id'] for row in data['shifts'][2:]]
    data['types']['u'] = copy.deepcopy(data['types']['t'])
    for row in data['shifts'][:3]:
        row['participants'][0]['supervisor_person_id'] = 'b'
    data['shifts'][3]['assignment_type_id'] = 'u'
    data['shifts'][3]['participants'] = data['shifts'][3]['participants'][1:]
    data['mentorships'] = [{'person_id': 'a', 'mentor_id': 'b', 'starts_on': '2020-01-01',
                            'ends_on': None, 'attachment': 'preferred'}]
    data['policies'] += [
        _rule('mixed', {'kind': 'allocation_preference', 'priority': 80}, scope={'conditions': {
            'match': match, 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': ['a']},
                                          {'field': 'assignment_type_ids', 'op': 'any', 'values': ['u']}]} }),
        _rule('unassigned', {'kind': 'allocation_preference', 'session_priority': 200},
              scope={'person_id': 'a', 'assignment_type_id': 'u'}),
        _rule('negative', {'kind': 'allocation_preference', 'priority': -5}, scope={'person_id': 'b'}),
        _rule('absent', {'kind': 'allocation_preference', 'priority': 500}, scope={'person_id': 'absent'}),
        _rule('offer', {'kind': 'allocation_overtime'}, scope={'person_id': 'a'}),
    ]
    return data


def item_receipt(data, symbolic):
    data = copy.deepcopy(data)
    participants = {(row['id'], person['person_id']): person
                    for row in data['shifts'] for person in row['participants']}
    choices = dict.fromkeys(participants, 1)
    choices[data['focus_ids'][0], 'c'] = 0
    model = cp_model.CpModel()
    if symbolic:
        for pair, value in list(choices.items()):
            if value:
                choices[pair] = model.new_bool_var('selected')
                model.add(choices[pair] == value)
        begin_score_program(model)
    items = allocation_score.allocation_score_items(
        data, choices, participants, math=ScoreModel(model) if symbolic else None)
    program = finish_score_program(model, {str(n): (item['loss'], item['active'])
                                          for n, item in enumerate(items)}, 1) if symbolic else None
    if symbolic:
        assert program is not None and program['ops']
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        solver.parameters.max_time_in_seconds = 2
        assert solver.solve(model) == cp_model.OPTIMAL
        value = solver.value
    else:
        value = lambda expression: expression
    rows = [{**item, 'loss': value(item['loss']), 'active': value(item['active'])} for item in items]
    return rows, str(model.proto), program


@pytest.mark.parametrize('symbolic', [False, True], ids=['observed', 'symbolic'])
@pytest.mark.parametrize('match', ['all', 'any'])
@pytest.mark.parametrize('balance', ['supervisor', 'supervisor_group'])
def test_index_preserves_order_opportunity_ceilings_and_historical_exposure(monkeypatch, symbolic, match, balance):
    data = allocation_snapshot(match, balance)
    actual = item_receipt(data, symbolic)
    # Disable only the new necessary filter; generation retains the canonical per-person predicate.
    monkeypatch.setattr(allocation_score, 'session_matches', lambda *args: True)
    expected = item_receipt(data, symbolic)
    assert actual == expected
    assert any(row['label'] == 'allocation exposure' and row['active'] for row in actual[0])
    assert any(row['label'] == 'missed positive allocation priority' and row['loss'] for row in actual[0])


@pytest.fixture(scope='module')
def indexed_work():
    sessions = [shift(f's{n}', 1 + n % 28, [], 'u' if n % 4 == 0 else 't') for n in range(240)]
    data = _snapshot(sessions)
    data['types']['u'] = copy.deepcopy(data['types']['t'])
    data['focus_ids'] = [row['id'] for row in sessions[:24]]
    data['people'].update({f'unassigned-{n}': {'display_name': f'Synthetic {n}', 'active': True}
                           for n in range(140)})
    data['policies'] = [_rule(f'p{n}', {'kind': 'allocation_preference', 'priority': -1},
                              scope={'assignment_type_id': 'u'}) for n in range(4)]
    calls = Counter()
    original = allocation_score.allocation_policy_matches

    def matched(view, policy, session, pid):
        calls['matches'] += 1
        assert session['id'] in data['focus_ids'] and session['assignment_type_id'] == 'u'
        return original(view, policy, session, pid)

    def check():
        calls['checks'] += 1

    data['_check_budget'] = check
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(allocation_score, 'allocation_policy_matches', matched)
        started = time.process_time()
        items = allocation_score.allocation_score_items(data)
        cpu = time.process_time() - started
    return items, calls, cpu


def test_negative_preferences_skip_unassigned_people_and_sessions(indexed_work):
    items, calls, _ = indexed_work
    assert items == []
    assert calls['matches'] == 0
    assert calls['checks'] <= 4 * (1 + 24 + 144 + 144 * 6)


@pytest.mark.cpu_budget
def test_indexed_allocation_cpu_budget(indexed_work):
    assert indexed_work[2] < python_budget(1), indexed_work[2]


@pytest.mark.parametrize('kind', ['mentor', 'overtime'])
def test_mentor_and_overtime_checks_do_not_rescan_outside_focus(kind):
    def run(history):
        data = _snapshot([shift('focus', 4, [])] + [shift(f'old-{n}', 3, []) for n in range(history)])
        data['focus_ids'] = ['focus']
        data['policies'] = [_rule('offer', {'kind': 'allocation_overtime'})] if kind == 'overtime' else []
        if kind == 'mentor':
            data['mentorships'] = [{'person_id': 'a', 'mentor_id': 'b', 'starts_on': '2020-01-01', 'ends_on': None}]
        checks = []
        data['_check_budget'] = lambda: checks.append(True)
        items = allocation_score.allocation_score_items(data)
        return items, len(checks)

    assert run(240) == run(0)


def test_nonmatching_session_preindex_checks_cancellation(monkeypatch):
    data = _snapshot([shift('first', 4, []), shift('second', 5, [])],
                     [_rule('other', {'kind': 'allocation_preference', 'priority': 1},
                            scope={'assignment_type_id': 'other'})])
    probes = []
    original = allocation_score.session_matches

    def matched(*args):
        probes.append(args[2]['id'])
        assert not original(*args)
        return False

    def check():
        if probes:
            raise TimeLimit()

    data['_check_budget'] = check
    monkeypatch.setattr(allocation_score, 'session_matches', matched)
    with pytest.raises(TimeLimit):
        allocation_score.allocation_score_items(data)
    assert probes == ['first']


@pytest.mark.parametrize('diagnostic', [False, True], ids=['strict', 'diagnostic'])
def test_complete_prepare_hashes_match_unfiltered_reference(monkeypatch, diagnostic):
    data = department()
    people = list(data['people'])[:4]
    data['people'] = {pid: data['people'][pid] for pid in people}
    data['shifts'] = [data['shifts'][n] for n in (0, 1, 15, 16)]
    for n, session in enumerate(data['shifts']):
        session.update(participants=[], locked=False)
        if n == 0:
            session['participants'] = [{'id': 'synthetic-history', 'person_id': people[0],
                'start': session['start'], 'end': session['end'], 'participation': 'staff', 'overtime': False}]
    data['focus_ids'] = [session['id'] for session in data['shifts'][2:]]
    data['availability'] = []
    for name in ('memberships', 'eligibilities'):
        data[name] = [row for row in data[name] if row['person_id'] in people]
    data['policies'] = data['policies'][20:23]
    data['fingerprint'] = 'synthetic-complete-allocation-index'

    def prepare():
        prepared = cp_sat._prepare(data, 1, 1, SolverControl(), GenerationOptions(), False, diagnostic)
        assert 'model' in prepared, prepared
        assert not prepared['model'].validate()
        assert prepared['score_program'] and prepared['evidence'] and prepared['before_score']
        values = {'model': str(prepared['model'].proto),
                  'complete_prepare': prepared_state('allocation-index', prepared),
                  **{key: prepared[key] for key in ('score_program', 'evidence', 'before_score')}}
        return {key: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
                for key, value in values.items()}

    actual = prepare()
    monkeypatch.setattr(allocation_score, 'session_matches', lambda *args: True)
    assert actual == prepare()
