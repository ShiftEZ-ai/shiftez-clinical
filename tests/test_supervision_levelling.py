"""Session-local supervision levelling; CI runs these on the supported toolchain."""

import time
from uuid import uuid4

import pytest

from packages.contracts.schemas import CatalogueRule, SpreadEvenly
from packages.domain.supervision_levelling import levelled_finding, levelled_pairing


def test_spread_evenly_is_a_catalogue_line_with_a_configurable_gap():
    line = {'id': str(uuid4()), 'numbers': {'max': {'value': 1, 'level': 'mandatory'}},
            'settings': {'spread_evenly': {'holder': 'supervisors', 'within': 'session', 'mode': 'levelled'}}}
    rule = CatalogueRule(kind='catalogue', id='spread_evenly', catalogue_version=2,
                         settings={'spread_evenly': {'holder': 'supervisors', 'within': 'session', 'mode': 'levelled'}},
                         lines=[line])
    assert rule.lines[0].numbers['max'].value == 1
    assert SpreadEvenly(kind='spread_evenly', mode='maximum_gap', maximum_gap=7).maximum_gap == 7
    assert SpreadEvenly(kind='spread_evenly', mode='levelled', maximum_gap=0).maximum_gap == 0
    with pytest.raises(ValueError, match='maximum gap of 1 or less'):
        SpreadEvenly(kind='spread_evenly', mode='levelled', maximum_gap=2)


def test_five_trainees_and_three_supervisors_level_to_two_two_one():
    result = levelled_pairing(['t1', 't2', 't3', 't4', 't5'], ['s1', 's2', 's3'])
    assert sorted(result['counts'].values()) == [1, 2, 2]
    assert result['reason'] is None


def test_seven_trainees_and_three_supervisors_level_to_three_two_two():
    result = levelled_pairing([f't{i}' for i in range(7)], ['s1', 's2', 's3'])
    assert sorted(result['counts'].values()) == [2, 2, 3]
    assert result['reason'] is None


def test_allowed_rank_restrictions_are_respected_before_levelling():
    eligible = {'t1': {'s1'}, 't2': {'s1', 's2'}, 't3': {'s2', 's3'}, 't4': {'s2', 's3'}}
    result = levelled_pairing(list(eligible), ['s1', 's2', 's3'], eligible=eligible)
    assert result['reason'] is None
    assert all(result['assignment'][trainee] in supervisors for trainee, supervisors in eligible.items())
    assert max(result['counts'].values()) - min(result['counts'].values()) <= 1


def test_capacity_cap_reports_impossible_levelled_assignment():
    result = levelled_pairing([f't{i}' for i in range(5)], ['s1', 's2', 's3'],
                              capacities={'s1': 1, 's2': 1, 's3': 1})
    assert result['reason'] == 'capacity'


def test_solver_and_validator_share_the_same_levelled_result():
    trainees, supervisors = [f't{i}' for i in range(5)], ['s1', 's2', 's3']
    result = levelled_pairing(trainees, supervisors)
    assert levelled_finding(trainees, supervisors, result['assignment']) is None
    skewed = {**result['assignment'], 't1': 's1', 't2': 's1', 't3': 's1'}
    finding = levelled_finding(trainees, supervisors, skewed)
    assert finding and finding['reason'] == 'imbalance'
    strict = levelled_finding(trainees, supervisors, result['assignment'], maximum_gap=0)
    assert strict and strict['reason'] == 'unavoidable_gap'


@pytest.mark.cpu_budget
def test_department_scale_pairing_helper_stays_bounded():
    began = time.process_time()
    for session in range(500):
        levelled_pairing([f't{session}-{i}' for i in range(5)], ['s1', 's2', 's3'])
    elapsed = time.process_time() - began
    # CI records the actual value; this generous budget catches accidental
    # rota-wide scans while remaining independent of CP-SAT search time.
    from tests.test_performance_budget import python_budget
    assert elapsed < python_budget(2)
