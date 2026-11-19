"""Small deterministic regressions for the classified domain repair roots."""
from copy import deepcopy

import pytest
from ortools.sat.python import cp_model

from packages.domain import leadership, validation
from packages.domain.cp_sat_score import ScoreModel, begin_score_program, finish_score_program
from tests.test_live_score_program import assert_program_matches


@pytest.mark.parametrize('negated', [False, True])
def test_boolean_positive_reuses_expression_and_keeps_live_score_program(negated):
    model = cp_model.CpModel()
    flag = model.new_bool_var('flag')
    begin_score_program(model)
    expression = 1 - flag if negated else flag
    positive = ScoreModel(model).positive(expression)
    assert len(model.proto.variables) == 1 and not model.proto.constraints
    roots = {'rules': (positive, 1)}
    assert finish_score_program(model, roots, 1) is not None
    assert_program_matches(model, roots)
    for value in (0, 1):
        trial = model.clone()
        trial.add(trial.get_bool_var_from_proto_index(flag.index) == value)
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        assert solver.solve(trial) == cp_model.OPTIMAL
        assert solver.value(positive) == (1 - value if negated else value)


def test_no_lead_obligation_avoids_person_qualification_work(h, monkeypatch):
    data = h.snapshot(h.shift(h.workspace())['workspace'])
    leadership.prepare(data)
    def unexpected(*args, **kwargs):
        pytest.fail('No leadership obligation should inspect person qualifications')
    monkeypatch.setattr(leadership, 'matches', unexpected)
    assert leadership.summary(data, data['shifts'][0]) is None
    assert not leadership.qualified(data, h.seed['people'][0], data['shifts'][0],
                                    data['shifts'][0]['start'], data['shifts'][0]['end'])


def test_coverage_boundaries_use_named_restriction_index(h, monkeypatch):
    data = h.snapshot(h.shift(h.workspace(), people=h.seed['people'][:1])['workspace'])
    p0, p1, _ = h.seed['people']
    policies = []
    for pid in (p0, p1):
        row = h.policy({'kind': 'restriction', 'weekdays': list(range(7)),
                        'start_time': '09:00', 'end_time': '10:00'}, scope={'person_id': pid})
        policies.append(row)
    data['policies'] = policies
    before = deepcopy(data)
    original = validation.applicable_restriction_intervals
    visited = []
    def probe(data, policy, pid, *args):
        assert pid == policy['definition']['scope']['person_id']
        visited.append((policy['id'], pid))
        return original(data, policy, pid, *args)
    monkeypatch.setattr(validation, 'applicable_restriction_intervals', probe)
    duty = data['shifts'][0]
    boundary_data = {**deepcopy(data), '_restriction_index_cache': {}}
    validation.coverage_boundaries(boundary_data, duty, duty['start'], duty['end'])
    assert visited and {pid for _, pid in visited} == {p0}
    report = validation.evaluate(data)
    assert any(f['rule_id'] == policies[0]['id'] for f in report['findings'])
    assert data == before


@pytest.mark.parametrize('worked', range(8))
def test_pattern_conjunction_uses_one_constraint_and_exact_boolean_cost(worked):
    from packages.domain.cp_sat_patterns import add_patterns
    from packages.domain.time import aware
    from tests.test_pattern_gap_construction import gap_input

    data = gap_input(3, limit=2)
    model = cp_model.CpModel()
    flags = [model.new_bool_var('duty') for _ in data['shifts']]
    rows = [(s, None, aware(s['start']), aware(s['end']), x) for s, x in zip(data['shifts'], flags)]
    begin_score_program(model)
    breaches = {}
    add_patterns(model, data, {'person': rows}, {'medium': 3}, wish_breaches=breaches)
    assert len(model.proto.constraints) == 1
    for i, flag in enumerate(flags):
        model.add(flag == ((worked >> i) & 1))
    roots = {'preferences': (1_000_000 * breaches['gap-wish'], 1)}
    assert_program_matches(model, roots)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    assert solver.value(breaches['gap-wish']) == (worked == 5)
