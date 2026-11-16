"""Standalone OR-Tools regression: presolve can reject a feasible integer model."""
from pathlib import Path

import ortools
from ortools.sat.python import cp_model


FIXTURE = Path(__file__).parent / 'fixtures/cpsat_false_infeasible_16.pbtxt'
KNOWN_AFFECTED_VERSION = '9.15.6755'
# A feasible assignment for every variable, including the unconstrained remnants.
WITNESS = (1, 0, 1, 1, 0, 0, 0, 0, 0, 7, 7, 0, 3, 3, 6, 285714,
           285714, 428571, 428571, 428571, 428571, 428571, 214285,
           561225224490, 561225, 0, 0, 0)


def _solve(model, probing=0, presolve=True):
    solver = cp_model.CpSolver()
    parameters = solver.parameters
    parameters.num_workers = 1
    parameters.max_time_in_seconds = 2
    parameters.random_seed = 1
    parameters.cp_model_probing_level = probing
    parameters.cp_model_presolve = presolve
    parameters.symmetry_level = 0
    parameters.max_presolve_iterations = 1
    parameters.find_big_linear_overlap = False
    return solver.solve(model)


def test_ortools_presolve_false_infeasibility_has_a_complete_feasible_witness(record_property):
    model = cp_model.CpModel()
    model.proto.parse_text_format(FIXTURE.read_text(encoding='utf-8'))
    assert model.validate() == ''
    assert len(model.proto.variables) == len(WITNESS) == 28
    assert len(model.proto.constraints) == 16
    assert not model.proto.assumptions and not model.has_objective()

    old = _solve(model)
    fixed = _solve(model, probing=2)
    confirmed = _solve(model, presolve=False)
    feasible = {cp_model.OPTIMAL, cp_model.FEASIBLE}
    record_property('ortools_version', ortools.__version__)
    record_property('old_parameters_status', old.name)
    record_property('old_parameter_bug_fixed', old in feasible)
    print(f'OR-Tools {ortools.__version__}: probing0={old.name}, '
          f'probing2={fixed.name}, presolve_disabled={confirmed.name}')
    assert fixed in feasible
    assert confirmed in feasible
    if ortools.__version__ == KNOWN_AFFECTED_VERSION:
        assert old == cp_model.INFEASIBLE
    else:
        # Upgrades may fix the old settings; retain their observed result in the report.
        assert old in feasible | {cp_model.INFEASIBLE}

    for index, value in enumerate(WITNESS):
        model.add(model.get_int_var_from_proto_index(index) == value)
    assert _solve(model) in feasible
