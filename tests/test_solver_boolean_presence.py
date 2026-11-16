"""Boolean presence stays exact without generic integer-max expansion."""
import copy
from itertools import product

from ortools.sat.python import cp_model
import pytest

from packages.domain.cp_sat import generate
from packages.domain.cp_sat_score import ScoreModel, begin_score_program
from packages.domain.validation import evaluate
from tests.test_engine_workload_review import one_person, policy


@pytest.mark.parametrize('shape', ['literals', 'duplicates', 'generator', 'affine', 'empty', 'constant'])
def test_score_presence_preserves_every_input_and_recorded_max(shape):
    model = cp_model.CpModel()
    x, y = model.new_bool_var('x'), model.new_bool_var('y')
    begin_score_program(model)
    operands = [x - y, 1 - x] if shape == 'affine' else [x, y, x] if shape == 'duplicates' else [x, y]
    if shape == 'generator':
        operands = (value for value in operands)
    elif shape == 'empty':
        operands = []
    elif shape == 'constant':
        operands = [x, 1]
    present = ScoreModel(model).any(operands)
    if shape in {'literals', 'duplicates', 'generator'}:
        assert len(model.proto.constraints) == (2 if shape == 'duplicates' else 1)
    assert any(constraint.has_lin_max() for constraint in model.proto.constraints) is (shape == 'affine')
    if shape not in {'empty', 'constant'}:
        assert model._score_program['ops'][-1][1] == 'max'
    for left, right in product((0, 1), repeat=2):
        pinned = model.clone()
        pinned.add(pinned.get_bool_var_from_proto_index(x.index) == left)
        pinned.add(pinned.get_bool_var_from_proto_index(y.index) == right)
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        assert solver.solve(pinned) == cp_model.OPTIMAL
        expected = (max(left - right, 1 - left) if shape == 'affine' else 0 if shape == 'empty'
                    else 1 if shape == 'constant' else max(left, right))
        assert solver.value(present) == expected


def test_work_presence_clauses_match_canonical_overlap_and_consecutive_day_rules(h):
    typ = h.configure('assignment_types', {'name': 'Synthetic optional presence', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'maximum': 1}]}})['record']['id']
    workspace = h.workspace(start='2026-10-04', end='2026-10-06')
    for day in (4, 4, 5, 5):
        workspace = h.shift(workspace, typ=typ, start=f'2026-10-{day:02}T08:00:00+03:00',
            end=f'2026-10-{day:02}T12:00:00+03:00', people=[])['workspace']
    person = h.seed['people'][0]
    data = one_person(h.snapshot(workspace), person)
    data['policies'] = [policy(data, {'kind': 'max_consecutive_days', 'maximum': 1})]
    captured = []
    result = generate(data, 1, feasibility_only=True, _on_model=captured.append)
    assert result['status'] == 'model_ready', result
    model, = captured
    flags = {index for index, variable in enumerate(model.proto.variables) if variable.name == 'work_present'}
    assert flags
    assert not any(constraint.has_lin_max() and any(index in flags for index in constraint.lin_max.target.vars)
                   for constraint in model.proto.constraints)
    choices = {shift['id']: next(index for index, variable in enumerate(model.proto.variables)
               if variable.name == shift['id'] + ':' + person) for shift in data['shifts']}
    for selected in product((0, 1), repeat=len(choices)):
        pinned, witness = model.clone(), copy.deepcopy(data)
        for shift, chosen in zip(witness['shifts'], selected):
            pinned.add(pinned.get_bool_var_from_proto_index(choices[shift['id']]) == chosen)
            if chosen:
                shift['participants'] = [{'id': shift['id'] + ':presence', 'shift_id': shift['id'],
                    'person_id': person, 'start': shift['start'], 'end': shift['end'],
                    'participation': 'staff', 'overtime': False, 'revision': 1}]
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        status = solver.solve(pinned)
        assert status in (cp_model.OPTIMAL, cp_model.INFEASIBLE)
        assert (status == cp_model.OPTIMAL) is evaluate(witness)['valid'], selected


def test_score_wide_boolean_presence_keeps_clauses_and_recorded_inputs():
    model = cp_model.CpModel()
    operands = [model.new_bool_var(name) for name in ('x', 'y', 'z')]
    begin_score_program(model)
    present = ScoreModel(model).any(operands)
    assert len(model.proto.variables) == 4 and len(model.proto.constraints) == 2
    assert model.proto.constraints[0].has_bool_or() and model.proto.constraints[1].has_bool_and()
    assert model._score_program['ops'][-1] == [present.index, 'max',
        [{'offset': 0, 'terms': [[value.index, 1]]} for value in operands]]
    for values in product((0, 1), repeat=3):
        pinned = model.clone()
        for variable, value in zip(operands, values):
            pinned.add(pinned.get_bool_var_from_proto_index(variable.index) == value)
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        assert solver.solve(pinned) == cp_model.OPTIMAL
        assert solver.value(present) == max(values)
