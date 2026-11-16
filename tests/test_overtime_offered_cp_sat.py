"""Offered-share objectives retain fractional hours without large second-based coefficients."""
import pytest
from ortools.sat.python import cp_model

from tests.test_overtime_offered import away, balance, goal_model, offer, snapshot


@pytest.mark.parametrize('metric', ['overtime_duties', 'overtime_hours'])
def test_cp_sat_offered_shares_use_minute_precision_for_partial_availability(metric):
    data = snapshot(2, last='2026-10-05')
    a, b = data['people']
    offer(data, a, 'minute-small', end='08:15')
    offer(data, b, 'minute-large', end='08:45')
    for pid in (a, b):
        away(data, pid, '2026-10-04T08:00:00', '2026-10-04T08:00:10')
    balance(data, metric)
    model, choices, terms, _ = goal_model(data, 8)
    # Sub-minute availability must not inflate an otherwise small 1:3 share objective.
    assert max(abs(coefficient) for constraint in model.proto.constraints
               for coefficient in constraint.linear.coeffs) <= 1000
    model.minimize(sum(terms))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5
    solver.parameters.num_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    assert {pid: sum(solver.value(x) for (_, who), x in choices.items() if who == pid)
            for pid in (a, b)} == {a: 2, b: 6}
