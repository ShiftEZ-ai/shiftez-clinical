"""Configured fill stages prefer existing demand without rewarding surplus work."""
import time

import pytest
from ortools.sat.python import cp_model

from packages.domain.cp_sat import generate as cp_generate
from packages.domain.generation import generate as greedy_generate
from packages.domain.fill_priority import FillPriority


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
def test_configured_fill_stage_precedes_candidate_preference_and_keeps_safety(h, engine):
    earlier, preferred = h.seed['people'][:2]
    h.policy({'kind': 'allocation_preference', 'priority': 0, 'session_priority': 10},
             {'person_id': earlier}, 'advisory')
    h.policy({'kind': 'allocation_preference', 'priority': 100}, {'person_id': preferred}, 'advisory')
    duty = h.configure('assignment_types', {'name': 'Single required place', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']
    workspace = h.shift(h.workspace(), people=[], typ=duty['id'])['workspace']
    data = h.snapshot(workspace)
    data['people'] = {pid: data['people'][pid] for pid in (earlier, preferred)}
    assert engine(data)['additions'][0]['participant']['person_id'] == preferred
    data['generation_options'] = {'allocation_order': 'tiered'}
    staged = engine(data)
    assert staged['status'] == 'candidate_feasible', staged
    assert [r['participant']['person_id'] for r in staged['additions']] == [earlier]
    data['availability'].append({'id': 'unavailable-fixture', 'person_id': earlier, 'kind': 'recorded_leave',
        'start': data['shifts'][0]['start'], 'end': data['shifts'][0]['end']})
    fallback = engine(data)
    assert fallback['status'] == 'candidate_feasible', fallback
    assert [r['participant']['person_id'] for r in fallback['additions']] == [preferred]


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
@pytest.mark.parametrize('metric, amount', [('duties', 1), ('reserved_hours', 4), ('session_units', 1)])
def test_fill_stage_uses_quota_demand_and_never_creates_unrequested_work(h, engine, metric, amount):
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_preference', 'priority': 0, 'session_priority': 10},
             {'person_id': person, 'service_ids': [h.seed['services'][1]]}, 'advisory')
    h.policy({'kind': 'allocation_preference', 'priority': 100},
             {'person_id': person, 'service_ids': [h.seed['services'][0]]}, 'advisory')
    duty = h.configure('assignment_types', {'name': 'Optional demand pool', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']
    workspace = h.shift(h.workspace(), people=[], typ=duty['id'])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00',
        people=[], typ=duty['id'], service=1)['workspace']
    data = h.snapshot(workspace)
    data['people'] = {person: data['people'][person]}
    data['generation_options'] = {'allocation_order': 'tiered'}
    empty = engine(data)
    assert empty['status'] == 'candidate_feasible', empty
    assert not empty['additions']
    if engine is cp_generate:
        assert not any(row['name'].startswith('fill:') for row in empty['objectives'])
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'metric': metric, 'comparison': 'target', 'amount': amount}, {'person_id': person}, 'advisory')
    quota_workspace = h.shift(h.workspace(), people=[], typ=duty['id'])['workspace']
    quota_workspace = h.shift(quota_workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00',
                               people=[], typ=duty['id'], service=1)['workspace']
    data = h.snapshot(quota_workspace)
    data['people'] = {person: data['people'][person]}
    data['generation_options'] = {'allocation_order': 'tiered'}
    result = engine(data)
    assert result['status'] == 'candidate_feasible', result.get('issues', result.get('validation'))
    assert len(result['additions']) == 1
    assert result['additions'][0]['participant']['start'].startswith('2026-10-05')


@pytest.mark.parametrize('extra', [0, 1])
def test_fill_stage_objective_never_improves_by_adding_surplus(extra):
    model = cp_model.CpModel()
    first, kept = model.new_bool_var('first'), model.new_constant(1)
    choices = {('duty', 'first'): first, ('duty', 'kept'): kept}
    priorities = FillPriority(model, {('duty', 'first'): 1, ('duty', 'kept'): 0}, include_unranked=True)
    priorities.target([(1, value, [(key, value)]) for key, value in choices.items()], 1)
    _, objective, _ = priorities.objectives()[0]
    model.add(first == extra)
    model.minimize(sum(objective))
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    assert solver.objective_value == 1000  # The kept person already meets the single required place.


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
def test_tiered_fill_leaves_satisfied_unbounded_staffing_alone(h, engine):
    earlier, kept = h.seed['people'][:2]
    h.policy({'kind': 'allocation_preference', 'priority': 0, 'session_priority': 10},
             {'person_id': earlier}, 'advisory')
    duty = h.configure('assignment_types', {'name': 'Required cover without a maximum', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}]}})['record']
    workspace = h.shift(h.workspace(), people=[kept], typ=duty['id'])['workspace']
    data = h.snapshot(workspace)
    data['generation_options'] = {'allocation_order': 'tiered'}
    result = engine(data)
    assert result['status'] == 'candidate_feasible', result
    assert result['additions'] == result['removals'] == []


@pytest.fixture(scope='module')
def department_fill_credit():
    model = cp_model.CpModel()
    shifts = {str(sid): {'id': str(sid)} for sid in range(500)}
    choices = {(sid, str(pid)): model.new_bool_var(f'{sid}:{pid}') for sid in shifts for pid in range(150)}
    started = time.process_time()
    priorities = FillPriority(model, {key: int(key[1]) % 3 for key in choices}, include_unranked=True)
    for sid in shifts:
        priorities.target([(1, choices[sid, str(pid)], [((sid, str(pid)), choices[sid, str(pid)])])
            for pid in range(150)], 3)
    elapsed = time.process_time() - started
    print('fill priority construction', {'cpu_seconds': elapsed, 'variables': len(model.proto.variables),
        'constraints': len(model.proto.constraints)})
    return model, priorities, len(choices), len(shifts), elapsed


def test_department_scale_fill_credit_construction_is_linear(department_fill_credit):
    model, priorities, places, shifts, _ = department_fill_credit
    assert len(model.proto.variables) < 2 * places + 5 * shifts
    assert len(model.proto.constraints) < 3 * places + 10 * shifts
    assert len(priorities.objectives()) == 3


@pytest.mark.cpu_budget
def test_department_scale_fill_credit_construction_cpu_budget(department_fill_credit):
    from tests.test_scheduler_performance import _budget
    elapsed = department_fill_credit[4]
    assert elapsed < _budget(20), elapsed
