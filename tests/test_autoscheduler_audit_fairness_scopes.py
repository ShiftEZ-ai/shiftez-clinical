"""CP-SAT fairness must count the same scoped duties as the shared goal report."""
from ortools.sat.python import cp_model
import pytest

from packages.domain.cp_sat_goals import add_balance_goals
from packages.domain.fairness import goal_report
from tests.test_engine_fairness_review import A, DEPT, duty, policy, uid, world


@pytest.mark.parametrize('nested', [False, True], ids=['flat-skill', 'nested-skill'])
@pytest.mark.parametrize('qualified_first', [False, True], ids=['unqualified-first', 'qualified-first'])
def test_solver_fairness_counts_clinic_specific_skills_per_session(nested, qualified_first):
    clinic, other_clinic, skill = [uid(n) for n in (40, 41, 42)]
    clinics = [clinic, other_clinic] if qualified_first else [other_clinic, clinic]
    data = world([duty(n, 15, [A], [service]) for n, service in enumerate(clinics, 1)])
    data['capabilities'] = {skill: {'active': True}}
    data['grants'] = [{'person_id': A, 'department_id': DEPT, 'capability_id': skill,
                       'service_id': clinic, 'starts_on': '2020-01-01'}]
    scope = {'person_ids': [A], **({'conditions': {'match': 'all', 'conditions': [
        {'match': 'all', 'conditions': [{'field': 'capability_ids', 'op': 'any', 'values': [skill]}]}]}}
        if nested else {'capability_ids': [skill]})}
    goal = policy({'metric': 'duties', 'period': 'rota', 'aim': 'target', 'amount': 1}, scope)
    data['policies'] = [goal]

    # Only the clinic covered by the skill grant earns a contribution, even for the same type and local day.
    [row] = goal_report(data, goal)['people']
    assert (row['planned'], row['expected']) == (1, 1)
    model, choices, participants = cp_model.CpModel(), {}, {}
    for shift in data['shifts']:
        [participant] = shift['participants']
        key = shift['id'], A
        choices[key] = model.new_bool_var(str(key))
        participants[key] = participant
        model.add(choices[key] == 1)
    terms, _ = add_balance_goals(model, data, choices, participants, {}, {}, set(data['focus_ids']), {'medium': 3})
    model.minimize(sum(terms))
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    assert solver.objective_value == 0
