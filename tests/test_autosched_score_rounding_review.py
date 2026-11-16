"""Live goal normalization cannot publish a different score from the saved rota."""
import json

import pytest
from ortools.sat.python import cp_model

from packages.domain.cp_sat_goals import add_score_goals
from packages.domain.cp_sat_score import (SCORE_SCALE, ScoreModel, begin_score_program, coverage_component,
                                          finish_score_program)
from packages.domain.score import compiled_progress_score, progress_score, schedule_score
from tests.test_engine_fairness_review import A, B, duty, policy, world
from tests.test_weighted_goals import STRENGTHS


OPTIONS = {'scoring': {'weights': {'coverage': 2, 'rules': 0, 'fairness': 19,
                                   'preferences': 0, 'stability': 0}}}


def goal_program(amount, *, record=True):
    data = world([duty(1, 5, [A]), duty(2, 6, [B])])
    data['generation_options'] = OPTIONS
    data['policies'] = [policy({'metric': 'duties', 'aim': 'target', 'period': 'rota', 'amount': amount,
                                'tolerance_percent': 0, 'tolerance_units': 0})]
    model, choices, participants = cp_model.CpModel(), {}, {}
    for session in data['shifts']:
        for person in (A, B):
            key = session['id'], person
            choices[key] = model.new_bool_var('choice')
            participants[key] = {'person_id': person, 'start': session['start'], 'end': session['end']}
    if record:
        begin_score_program(model)
    presence = []
    terms = add_score_goals(model, data, choices, participants, {}, {}, set(data['focus_ids']), STRENGTHS,
                            presence=presence)
    q = ScoreModel(model)
    roots = {'coverage': coverage_component(model, [
        (sum(choices[session['id'], person] for person in (A, B)), 1, (), 1) for session in data['shifts']]),
        'fairness': (q.mean(terms), q.any(presence))}
    assigned = q.any(choices.values())
    program = finish_score_program(model, roots, assigned) if record else None
    for session in data['shifts']:
        for person in (A, B):
            model.add(choices[session['id'], person] == int(any(
                row['person_id'] == person for row in session['participants'])))
    return data, model, program, roots


@pytest.mark.parametrize('amount, ambiguous', [
    (1.0062893081761, True), (1.00628, False), (1.0064, False)])
def test_recorded_goal_rounding_checks_canonical_cost_before_publication(amount, ambiguous):
    data, model, program, roots = goal_program(amount)
    _, plain, _, _ = goal_program(amount, record=False)
    assert str(model.proto) == str(plain.proto)
    assert program is not None and program['goal_cost_checks']
    program = json.loads(json.dumps(program))
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = 2
    assert solver.solve(model) == cp_model.OPTIMAL
    solution = list(solver.response_proto.solution)[:program['prefix_size']]
    solution += [123456789] * len(model.proto.variables)
    checked = {'coverage': [{**{key: session[key] for key in ('start', 'end')},
                            'shift_id': session['id'], 'required': 1, 'available_eligible': 1, 'status': 'covered'}
                           for session in data['shifts']], 'findings': []}
    canonical = progress_score(schedule_score(data, checked))
    actual = compiled_progress_score(program, solution, OPTIONS)
    if ambiguous:
        assert canonical['total'] == 99.1 and canonical['components']['fairness'] == .9906
        assert round(1 - solver.value(roots['fairness'][0]) / SCORE_SCALE, 4) == .9907
        unchecked = compiled_progress_score({**program, 'goal_cost_checks': []}, solution, OPTIONS)
        assert unchecked['total'] == 99.2 and unchecked['components']['fairness'] == .9907
        assert actual is None
    else:
        assert actual == canonical


def test_legacy_score_program_without_goal_rounding_checks_is_unknown():
    program = {'prefix_size': 0, 'scale': SCORE_SCALE, 'ops': [], 'assigned': 1,
               'components': {'coverage': [0, 1]}, 'goal_cost_checks': []}
    assert compiled_progress_score(program, [])['total'] == 100
    del program['goal_cost_checks']
    assert compiled_progress_score(program, []) is None
