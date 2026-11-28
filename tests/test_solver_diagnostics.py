import pytest
from ortools.sat.python import cp_model
from packages.domain.solver_control import SolverControl, Cancelled
from packages.domain.solver_diagnostics import explain_conflict


def test_empty_candidate_quota_identifies_its_rule_without_mutating_model():
    model = cp_model.CpModel()
    model.add(sum([]) >= 4).with_name('policy:weekly')
    before = str(model.proto)
    data = {'fingerprint': 'frozen', 'policies': [{
        'id': 'weekly', 'revision': 2, 'name': 'Four clinical duties',
        'starts_on': '2026-09-06', 'definition': {'amount': 4}}]}
    control = SolverControl()
    control.solve = lambda *args: pytest.fail('A constant contradiction needs no search')
    result = explain_conflict(model, data, control, 1)
    assert result['proven'] and result['code'] == 'CONFLICTING_REQUIREMENTS'
    assert [r['rule_id'] for r in result['requirements']] == ['weekly']
    assert result['message'] == ('These requirements cannot all be met together with eligibility, availability '
                                 'and the other mandatory rules: Four clinical duties')
    assert not result['minimal'] and not result['exhaustive']
    assert 'may not be the only cause' in result['boundary']
    assert str(model.proto) == before


@pytest.mark.parametrize('status, implementation_error', [
    (cp_model.UNKNOWN, False), (cp_model.MODEL_INVALID, True),
    (cp_model.FEASIBLE, True), (cp_model.OPTIMAL, True)])
def test_unproven_diagnostic_outcomes_never_claim_a_conflict_core(status, implementation_error):
    model = cp_model.CpModel()
    control = SolverControl()
    control.solve = lambda *args: status
    result = explain_conflict(model, {'fingerprint': 'frozen'}, control, 1)
    assert result['implementation_error'] is implementation_error
    assert not result['proven'] and not result['requirements']
    assert result['proof_kind'] == 'unresolved'
    # A timed-out diagnosis keeps the confirmed strict result and says the cause is unknown.
    assert result['strict_status'] == 'INFEASIBLE'
    assert ('The cause is unknown' in result['message']) is (status == cp_model.UNKNOWN)


def test_background_conflict_is_not_presented_as_a_named_explanation():
    model = cp_model.CpModel()
    x = model.new_bool_var('x')
    model.add(x == 0)
    model.add(x == 1)
    before = str(model.proto)
    result = explain_conflict(model, {'fingerprint': 'frozen'}, SolverControl(), 1)
    assert result['solver_status'] == 'INFEASIBLE'
    assert not result['proven'] and not result['requirements']
    assert result['timing']['stop_reason'] == 'no_named_set'
    assert str(model.proto) == before
    with pytest.raises(Cancelled):
        explain_conflict(model, {'fingerprint': 'frozen'}, SolverControl(cancelled=lambda: True), 1)
