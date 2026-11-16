"""Zero-cost scheduling models still need an honest feasibility result."""
from ortools.sat.python import cp_model

from packages.domain.cp_sat import generate
from packages.domain.solver_control import SolverControl
from packages.domain.validation import evaluate


def optional_locked_snapshot(h):
    typ = h.configure('assignment_types', {
        'name': 'Optional locked session', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'maximum': 1}]},
    })['record']['id']
    workspace = h.shift(h.workspace(), typ=typ, people=[])['workspace']
    data = h.snapshot(workspace)
    data['shifts'][0]['locked'] = True
    return data


def test_locked_optional_session_returns_a_valid_noop_with_zero_objectives(h):
    data = optional_locked_snapshot(h)
    assert evaluate(data)['valid']

    result = generate(data, 3, deterministic=True)

    assert result['status'] == 'candidate_feasible', result
    assert result['validation']['valid']
    assert result['additions'] == result['removals'] == []
    assert [tier['name'] for tier in result['objectives']] == [
        'coverage', 'weighted',
    ]
    assert all(tier['value'] == 0 and tier['proven_optimal'] for tier in result['objectives'])
    assert result['optimality_proven']


def test_zero_objectives_do_not_hide_an_infeasible_background_constraint(h, monkeypatch):
    from packages.domain import cp_sat_workload

    data = optional_locked_snapshot(h)
    add_workload_limits = cp_sat_workload.add_workload_limits

    def contradictory_background(model, *args):
        add_workload_limits(model, *args)
        # An unnamed hard constraint represents fixed background requirements.
        # A zero objective cannot certify their feasibility without searching.
        model.add(False)

    monkeypatch.setattr(cp_sat_workload, 'add_workload_limits', contradictory_background)
    result = generate(data, 3, deterministic=True)

    assert result['status'] == 'requirements_conflict', result
    assert not result.get('optimality_proven', False)
    assert result['additions'] == result['removals'] == []


def test_zero_objectives_do_not_turn_an_unknown_search_into_a_proof(h, monkeypatch):
    data = optional_locked_snapshot(h)
    monkeypatch.setattr(SolverControl, 'solve', lambda *args: cp_model.UNKNOWN)

    result = generate(data, 3)

    assert result['status'] == 'needs_more_time', result
    assert not result.get('optimality_proven', False)
    assert not any(tier['proven_optimal'] for tier in result['objectives'])
    assert result['additions'] == result['removals'] == []
