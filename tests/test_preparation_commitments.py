"""Preparation shares immutable supervision facts without changing chosen assignments."""
import copy
import time

import pytest

from packages.contracts.schemas import GenerationOptions, Scope
from packages.domain import allocation_score, cp_sat, generation, validation
from packages.domain.solver_control import SolverControl
from tests.test_schedule_score_scale import department
from tests.test_scheduler_performance import _budget


def snapshot(sessions=450, people=150):
    data = department()
    data['people'] = dict(list(data['people'].items())[:people])
    data['shifts'] = data['shifts'][:sessions]
    data['focus_ids'] = [shift['id'] for shift in data['shifts']]
    data['fingerprint'] = 'synthetic-preparation-commitments'
    data['availability'] = []
    data['policies'] = [p for p in data['policies'] if p['kind'] != 'restriction']
    for shift in data['shifts']:
        shift['locked'] = False
        shift['participants'] = [p for p in shift['participants'] if p['person_id'] in data['people']]
    return data


@pytest.fixture(scope='module')
def prepared_department():
    data, calls = snapshot(), []
    original = validation.supervision_commitments

    def counted(view):
        calls.append(len(view['shifts']))
        return original(view)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(validation, 'supervision_commitments', counted)
        patch.setattr(cp_sat, 'supervision_commitments', counted)
        started = time.process_time()
        prepared = cp_sat._prepare(data, 10, 1, SolverControl(), GenerationOptions(), True, False)
        elapsed = time.process_time() - started
    print('supervision preparation', {'cpu_seconds': elapsed, 'builds': len(calls), 'people': 150, 'sessions': 450})
    return prepared, calls, elapsed


def test_preparation_builds_supervision_commitments_once_before_candidates(prepared_department):
    prepared, calls, _ = prepared_department
    assert 'model' in prepared, prepared.get('status')
    assert len(calls) <= 2, calls
    assert len(prepared['evidence']) == 150 * 450


@pytest.mark.cpu_budget
def test_preparation_supervision_department_cpu_budget(prepared_department, record_property):
    record_property('cpu_seconds', prepared_department[2])
    assert prepared_department[2] < _budget(10), prepared_department[2]


@pytest.mark.parametrize('audience', ['named', 'condition', 'mixed-any', 'everyone'])
def test_overtime_preparation_indexes_named_people_and_keeps_wildcards(monkeypatch, audience):
    data = snapshot(sessions=2, people=25)
    people = list(data['people'])
    data['policies'] = [p for p in data['policies'] if p['kind'] == 'allocation_overtime']
    scope = {'person_ids': people[:1]} if audience == 'named' else {}
    if audience in {'condition', 'mixed-any'}:
        scope = {'conditions': {'match': 'all' if audience == 'condition' else 'any',
            'conditions': [{'field': 'person_ids', 'values': people[:1]},
                           {'field': 'service_ids', 'values': data['shifts'][0]['service_ids']}]}}
    data['policies'][0]['definition']['scope'] = Scope.model_validate(scope).model_dump(mode='json')
    expected = {(shift['id'], pid): generation.allocation_settings(data, shift, pid)[1]
                for shift in data['shifts'] for pid in [*people, None]}
    original, calls = generation.allocation_policy_matches, []

    def matched(view, policy, shift, pid):
        if pid is not None:
            calls.append((policy['id'], pid))
        return original(view, policy, shift, pid)

    monkeypatch.setattr(generation, 'allocation_policy_matches', matched)
    for shift in data['shifts']:
        for pid in [*people, None]:
            assert generation.allocation_overtime_allowed(data, shift, pid) == expected[shift['id'], pid]
    assert len(calls) <= 2 * len(people) * len(data['shifts']), len(calls)
    data['policies'] = []
    assert not generation.allocation_overtime_allowed(data, data['shifts'][0], people[0])


def test_allocation_preparation_filters_sessions_before_people_without_changing_score_items(monkeypatch):
    data, calls = snapshot(sessions=12, people=25), []
    original = allocation_score.allocation_policy_matches

    def matched(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(allocation_score, 'allocation_policy_matches', matched)
    actual = allocation_score.allocation_score_items(copy.deepcopy(data))
    filtered_calls = len(calls)
    calls.clear()
    monkeypatch.setattr(allocation_score, 'session_matches', lambda *args: True)
    reference = allocation_score.allocation_score_items(copy.deepcopy(data))
    assert actual == reference
    assert filtered_calls * 2 < len(calls), (filtered_calls, len(calls))


def test_cached_preparation_keeps_uncached_deterministic_assignments(monkeypatch):
    data = snapshot(sessions=6, people=20)
    cached = cp_sat.generate(copy.deepcopy(data), 3, feasibility_only=True, deterministic=True)
    from packages.domain.generation import allocation_settings

    def uncached(view, *args):
        return allocation_settings({key: value for key, value in view.items() if key != '_supervision_commitments'}, *args)[1]

    monkeypatch.setattr(cp_sat, 'allocation_overtime_allowed', uncached)
    reference = cp_sat.generate(copy.deepcopy(data), 3, feasibility_only=True, deterministic=True)
    assert cached['status'] == reference['status'] == 'candidate_feasible'
    assert cached['additions'] == reference['additions']
    assert cached['removals'] == reference['removals']
    assert cached['validation'] == reference['validation']


def test_staffing_commitments_refresh_when_an_indexed_seat_moves(monkeypatch):
    from apps.api.services import staffing
    from packages.domain.generation import allocation_settings

    data, calls = snapshot(sessions=6, people=20), []
    original = validation.supervision_commitments

    def counted(view):
        calls.append(1)
        return original(view)

    monkeypatch.setattr(validation, 'supervision_commitments', counted)
    index = staffing.people_index(data)
    for pid in data['people']:
        allocation_settings(index['fast'], data['shifts'][0], pid)
    assert len(calls) == 1
    shift = data['shifts'][0]
    person = next(pid for pid in data['people'] if pid not in {p['person_id'] for p in shift['participants']})
    staffing.move_seat(index, shift, shift['participants'][0], person)
    assert len(calls) == 2
    assert index['fast']['_supervision_commitments'] == original(index['fast'])
# Include this file in hosted supervision and team-lead qualification.
