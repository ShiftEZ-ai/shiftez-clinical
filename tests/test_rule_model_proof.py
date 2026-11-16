"""Structural rule proof uses the scheduler compiler without running the scheduler."""
import copy

import pytest
from ortools.sat.python import cp_model

from packages.domain import cp_sat, score, solver_diagnostics
from packages.domain.rule_groups import canonical_model, same_model


def test_model_proof_builds_without_search_or_global_validate_mutation(h, monkeypatch):
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    original_data = copy.deepcopy(data)
    validate, generate = cp_model.CpModel.validate, cp_sat.generate

    def forbidden(*args, **kwargs):
        pytest.fail('Structural model proof must not search, diagnose or score a generated schedule')

    def observed(*args, **kwargs):
        assert cp_model.CpModel.validate is validate
        return generate(*args, **kwargs)

    monkeypatch.setattr(cp_model.CpSolver, 'solve', forbidden)
    monkeypatch.setattr(solver_diagnostics, 'explain_conflict', forbidden)
    monkeypatch.setattr(score, 'schedule_score', forbidden)
    monkeypatch.setattr(cp_sat, 'generate', observed)
    proof = canonical_model(data)
    assert proof[0] == 'model_ready' and proof[1] and proof[2]
    assert proof[3] == [[], 0.0, 1.0]
    assert same_model(proof, canonical_model(data), {})
    assert cp_model.CpModel.validate is validate and data == original_data


@pytest.mark.parametrize('failure', ['unsupported_rules', 'unsupported_size', 'invalid_inputs', 'cancelled', 'model_invalid'])
def test_model_capture_preserves_preflight_and_validation_gates(h, monkeypatch, failure):
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    options = {}
    if failure == 'unsupported_rules':
        data['policies'].append({'id': 'unsupported', 'revision': 1, 'status': 'active', 'active': True,
                                 'definition': {'rule': {'kind': 'unsupported_for_test'}}})
    elif failure == 'unsupported_size':
        data['generation_session_ids'] = ['outside-this-rota']
    elif failure == 'invalid_inputs':
        data['types'][data['shifts'][0]['assignment_type_id']]['active'] = False
    elif failure == 'cancelled':
        options['cancelled'] = lambda: True
    else:
        class InvalidModel(cp_model.CpModel):
            def __init__(self):
                super().__init__()
                self.new_int_var(1, 0, 'invalid-empty-domain')
        monkeypatch.setattr(cp_model, 'CpModel', InvalidModel)

    captured = []
    result = cp_sat.generate(data, 5, feasibility_only=True, _on_model=captured.append, **options)
    assert result['status'] == failure and captured == []
    failed = (failure, [], [], [])
    assert not same_model(failed, failed, {})
    assert not same_model(failed, failed, {'old': 'new'})
    if failure != 'cancelled':
        assert canonical_model(data)[0] == failure


def test_model_capture_requires_feasibility_compilation(h):
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    with pytest.raises(ValueError, match='Model capture requires feasibility-only'):
        cp_sat.generate(data, 5, _on_model=lambda model: pytest.fail('Unexpected capture'))


@pytest.mark.parametrize('metadata', [False, True])
def test_model_comparison_keeps_constraints_variables_objective_and_rule_id_mapping(monkeypatch, metadata):
    def compiled(data, *args, _on_model, **kwargs):
        model = cp_model.CpModel()
        value = model.new_int_var(0, data.get('domain', 3), 'value:' + data['rule_id'])
        from packages.domain.solver_provenance import policy_name
        name = policy_name({'id': data['rule_id'], 'name': 'Source', 'revision': 1,
            'definition': {'rule': {'kind': 'max_duties', 'maximum': data.get('limit', 2)}}}, data=data)
        model.add(value <= data.get('limit', 2)).with_name(name if metadata else 'policy:' + data['rule_id'])
        model.minimize(data.get('coefficient', 1) * value)
        assert not model.validate()
        _on_model(model)
        return {'status': 'model_ready'}

    monkeypatch.setattr(cp_sat, 'generate', compiled)
    before = canonical_model({'rule_id': 'old'})
    after = canonical_model({'rule_id': 'new'})
    assert before[3][0]  # exercise nonempty objective terms, not only the feasibility model's zero objective
    assert same_model(before, after, {'old': 'new'})
    for change in ({'domain': 4}, {'limit': 1}, {'coefficient': 2}):
        assert not same_model(before, canonical_model({'rule_id': 'new', **change}), {'old': 'new'})
    failed = ('model_invalid', [], [], [])
    assert not same_model(before, failed, {'old': 'new'})
    assert not same_model(failed, after, {'old': 'new'})


@pytest.fixture
def proof_of_model(monkeypatch):
    def compiled(model, *args, _on_model, **kwargs):
        assert not model.validate()
        _on_model(model)
        return {'status': 'model_ready'}

    monkeypatch.setattr(cp_sat, 'generate', compiled)
    return canonical_model


@pytest.mark.parametrize('attachment', ['enforcement', 'negative_enforcement', 'linear', 'literal', 'objective'])
def test_model_comparison_preserves_duplicate_helper_identity(proof_of_model, attachment):
    def model_for(use_second):
        model = cp_model.CpModel()
        x, y, z = [model.new_bool_var(name) for name in ('input_x', 'input_y', 'result_z')]
        first, second = [model.new_bool_var('work_present') for _ in range(2)]
        assert str(model.proto.variables[first.index]) == str(model.proto.variables[second.index])
        model.add(first == x)
        model.add(second == y)
        model.add(x == 1)
        model.add(y == 0)
        model.add(z == 0)
        helper = second if use_second else first
        model.minimize(0)
        if attachment == 'enforcement':
            # Enforcing on first makes z both 0 and 1; enforcing on second leaves it feasible.
            model.add(z == 1).only_enforce_if(helper)
        elif attachment == 'negative_enforcement':
            model.add(z == 1).only_enforce_if(~helper)
        elif attachment == 'linear':
            model.add(z == helper)
        elif attachment == 'literal':
            model.add_bool_or([helper, z])
        else:
            model.minimize(3 * helper + z)
        return model

    before = proof_of_model(model_for(False))
    assert same_model(before, proof_of_model(model_for(False)), {})
    assert not same_model(before, proof_of_model(model_for(True)), {})


def test_model_comparison_preserves_unique_variable_reordering_and_rule_remap(proof_of_model):
    def model_for(rule_id, reverse):
        model = cp_model.CpModel()
        entries = [('x', 1), ('y', 0)]
        if reverse:
            entries.reverse()
        values = {name: model.new_bool_var(name + ':' + rule_id) for name, _ in entries}
        for name, value in entries:
            model.add(values[name] == value).with_name('policy:' + rule_id)
        model.minimize(3 * values['x'] + 7 * values['y'])
        return model

    before = proof_of_model(model_for('old-rule', False))
    after = proof_of_model(model_for('new-rule', True))
    assert same_model(before, after, {'old-rule': 'new-rule'})


@pytest.mark.parametrize('attachment', ['enforcement', 'negative_enforcement', 'linear', 'literal', 'objective'])
def test_max_defined_helpers_can_reorder_without_losing_their_identity(proof_of_model, attachment):
    def model_for(reverse, use_other=False):
        model = cp_model.CpModel()
        x, y, z = [model.new_bool_var(name) for name in ('input_x', 'input_y', 'result_z')]
        entries = [('x', x), ('y', y)]
        helpers = {}
        for key, value in reversed(entries) if reverse else entries:
            helpers[key] = model.new_bool_var('cover_trigger')
            model.add_max_equality(helpers[key], [value])
        model.add(x == 1)
        model.add(y == 0)
        model.add(z == 0)
        helper = helpers['y' if use_other else 'x']
        model.minimize(0)
        if attachment == 'enforcement':
            model.add(z == 1).only_enforce_if(helper)
        elif attachment == 'negative_enforcement':
            model.add(z == 1).only_enforce_if(~helper)
        elif attachment == 'linear':
            model.add(z == helper)
        elif attachment == 'literal':
            model.add_bool_or([helper, z])
        else:
            model.minimize(3 * helper + z)
        return model

    before = proof_of_model(model_for(False))
    assert same_model(before, proof_of_model(model_for(True)), {})
    assert not same_model(before, proof_of_model(model_for(True, use_other=True)), {})


def test_model_comparison_rejects_variable_identity_collisions_after_remapping(proof_of_model):
    def model_for(rule_ids):
        model = cp_model.CpModel()
        values = [model.new_bool_var('value:' + rule_id) for rule_id in rule_ids]
        for value in values:
            model.add(value == 1)
        model.minimize(0)
        return model

    before = proof_of_model(model_for(['old-one', 'old-two']))
    after = proof_of_model(model_for(['new-rule', 'new-rule']))
    assert same_model(after, after, {})
    assert not same_model(before, after, {'old-one': 'new-rule', 'old-two': 'new-rule'})
    # A proof whose variable inventory is already ambiguous cannot prove even itself equal.
    ambiguous = (after[0], after[1], [after[2][0], after[2][0]], after[3])
    assert not same_model(ambiguous, ambiguous, {})


def deficit_model(reverse=False, change=None, attachment=None, use_other=False):
    model = cp_model.CpModel()
    x, y, z = [model.new_bool_var(name) for name in ('input_x', 'input_y', 'result_z')]
    trigger = model.new_bool_var('cover_trigger')
    model.add_max_equality(trigger, [y if change == 'trigger' else x])
    entries = [('conditional', trigger), ('empty', 0)]
    if change == 'multiplicity':
        entries.append(('extra', 0))
    deficits = {}
    for key, expression in reversed(entries) if reverse else entries:
        domain = 2 if key == 'conditional' and change == 'domain' else 1
        deficits[key] = model.new_int_var(0, domain, 'preference_deficit')
        coefficient = 2 if key == 'conditional' and change == 'coefficient' else 1
        bound = -1 if key == 'conditional' and change == 'bound' else 0
        model.add(coefficient * expression <= deficits[key] + bound)
    helper = deficits['empty' if use_other else 'conditional']
    model.add(x == 1)
    model.add(y == 0)
    model.add(z == 0)
    model.minimize(0)
    if attachment == 'linear':
        model.add(z == helper)
    elif attachment == 'enforcement':
        model.add(z == 1).only_enforce_if(helper)
    elif attachment == 'negative_enforcement':
        model.add(z == 1).only_enforce_if(~helper)
    elif attachment == 'literal':
        model.add_bool_or([helper, z])
    elif attachment == 'objective':
        model.minimize(3 * helper)
    return model


def test_advisory_deficit_leaves_can_reorder_without_changing_proof(proof_of_model):
    before = proof_of_model(deficit_model())
    after = proof_of_model(deficit_model(reverse=True))
    assert same_model(before, after, {})


@pytest.mark.parametrize('change', ['trigger', 'coefficient', 'bound', 'domain', 'multiplicity'])
def test_advisory_deficit_identity_preserves_its_whole_constraint(proof_of_model, change):
    assert not same_model(proof_of_model(deficit_model()), proof_of_model(deficit_model(change=change)), {})


@pytest.mark.parametrize('attachment', ['linear', 'enforcement', 'negative_enforcement', 'literal', 'objective'])
def test_advisory_deficit_extra_attachments_keep_distinct_identity(proof_of_model, attachment):
    before = proof_of_model(deficit_model(attachment=attachment))
    assert same_model(before, proof_of_model(deficit_model(attachment=attachment)), {})
    assert not same_model(before, proof_of_model(deficit_model(attachment=attachment, use_other=True)), {})


@pytest.mark.parametrize('command', ['catalogue', 'groups'])
@pytest.mark.parametrize('identical', [False, True])
def test_proof_summaries_distinguish_unestablished_equivalence_from_a_difference(command, identical):
    from scripts.catalogue_rules import proof_lines as catalogue_proof
    from scripts.group_rules import proof_lines as group_proof
    proof = {'identical': identical, 'solver_inputs_equal': True, 'rules_before': 2, 'rules_after': 1,
        'rotas': [{'name': 'Rota awaiting proof', 'status': 'draft', 'model_equal': identical,
            'findings_only_before': [], 'findings_only_after': [], 'findings': 0, 'findings_after': 0,
            'quota_equal': True, 'solver_inputs_equal': True, 'quota_rows': 0, 'score_before': 100, 'score_after': 100}]}
    rendered = (catalogue_proof if command == 'catalogue' else group_proof)(proof)
    if identical:
        assert 'IDENTICAL' in rendered and 'CP-SAT model identical' in rendered
        assert 'NOT ESTABLISHED' not in rendered
    else:
        assert 'EQUIVALENCE NOT ESTABLISHED' in rendered and 'CP-SAT model not confirmed identical' in rendered
        assert 'DIFFERENT' not in rendered and 'DIFFERENCES FOUND' not in rendered
