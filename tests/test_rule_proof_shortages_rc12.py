"""Full-model proof captures infeasibility without relaxing cover or changing scheduling admission."""
import copy

import pytest
from ortools.sat.python import cp_model

from packages.domain import cp_sat
from packages.domain.rule_catalogue_migration import plan_v2, after_snapshot
from packages.domain.rule_migration_proof import canonical_full_model, strict_proof
from .test_rule_catalogue_v2_proof import fixture, source


@pytest.mark.parametrize('scoped', [False, True])
def test_exact_objective_capture_keeps_staffing_shortages_hard(h, scoped, monkeypatch):
    data = fixture(h, people=22, sessions=2 if scoped else 1)
    data['people'] = {}
    for shift in data['shifts']:
        shift['participants'] = []
    data['policies'] = [source(6800, {'kind': 'max_duties', 'maximum': 5, 'window': 'week'})]
    data['policies'][0]['department_id'] = data['workspace']['department_id']
    if scoped:
        data['generation_session_ids'] = [data['focus_ids'][0]]
    normal = cp_sat.generate(copy.deepcopy(data), 1, 1, deterministic=True)
    assert normal['status'] == 'requirements_conflict' and normal['diagnostics']
    retained = []
    original = cp_sat.keep_no_worse

    def no_worse(model, exempt, exempt_indexes=()):
        retained.append(exempt)
        return original(model, exempt, exempt_indexes)

    monkeypatch.setattr(cp_sat, 'keep_no_worse', no_worse)
    captured = []

    def capture(model, tiers):
        captured.append(model.clone())
        assert tiers and model.validate() == ''

    result = cp_sat.generate(copy.deepcopy(data), 1, 1, deterministic=True, _on_objectives=capture)
    assert result['status'] == 'model_ready' and result['optimality_proven'] is False
    if scoped:
        assert retained and 'duty:' + data['focus_ids'][0] in retained[0]
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 1
    solver.parameters.num_search_workers = 1
    assert solver.solve(captured[0]) == cp_model.INFEASIBLE
    plan = plan_v2(data['policies'], department_id=data['workspace']['department_id'])
    after = after_snapshot(data, plan)
    assert strict_proof(data, after)['identical']
    changed = copy.deepcopy(after)
    changed['shifts'][0]['type_snapshot']['definition']['requirements'][0]['minimum'] = 0
    assert canonical_full_model(data)['digest'] != canonical_full_model(changed)['digest']
