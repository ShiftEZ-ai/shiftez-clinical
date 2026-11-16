"""Duty requirements validate at writes and tolerate an omitted optional end in stored data."""
import copy

import pytest
from pydantic import ValidationError

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import TypeDefinition
from packages.domain.candidate_evidence import direct_coverage_shortages
from packages.domain.cp_sat import generate
from packages.domain.time import aware
from packages.domain.validation import evaluate


def duty_type(h, requirement):
    return h.configure('assignment_types', {
        'name': 'Requirement regression', 'starts_on': '2020-01-01',
        'definition': {'requirements': [requirement]},
    })['record']


def test_requirement_without_end_is_normalized_on_create_and_update(h):
    typ = duty_type(h, {'minimum': 1})
    assert typ['definition']['requirements'][0]['to_minute'] is None
    typ = h.configure('assignment_types', {
        'name': typ['name'], 'starts_on': typ['starts_on'],
        'definition': {'requirements': [{'from_minute': 60, 'minimum': 2}]},
    }, typ['id'], typ['revision'])['record']
    with transaction(h.app.state.engine) as db:
        requirement = db.get(m.AssignmentType, typ['id']).definition['requirements'][0]
        assert requirement['from_minute'] == 60
        assert requirement['to_minute'] is None
        assert requirement['minimum'] == 2


@pytest.mark.parametrize('requirement', [
    {'from_minute': 120, 'to_minute': 120},
    {'from_minute': 120, 'to_minute': 60},
    {'from_minute': 720},
    {'to_minute': 721},
])
def test_requirement_invalid_interval_rejected_by_contract_and_api(h, requirement):
    with pytest.raises(ValidationError):
        TypeDefinition(requirements=[requirement])
    value = {'name': 'Invalid requirement', 'starts_on': '2020-01-01',
             'definition': {'requirements': [requirement]}}
    h.configure('assignment_types', value, status=422)
    typ = duty_type(h, {'minimum': 1})
    h.configure('assignment_types', value, typ['id'], typ['revision'], status=422)
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.AssignmentType, typ['id'])
        assert stored.revision == typ['revision']
        assert stored.definition == typ['definition']


def test_stored_requirement_missing_end_uses_session_end_in_checks_and_solver(h):
    typ = duty_type(h, {'minimum': 1})
    result = h.shift(h.workspace(), typ=typ['id'], people=[])
    workspace = result['workspace']
    sid = h.snapshot(workspace)['shifts'][0]['id']
    # Reproduce legacy/direct storage without normalizing or rewriting its optional field.
    with transaction(h.app.state.engine, write=True) as db:
        stored_type = db.get(m.AssignmentType, typ['id'])
        definition = copy.deepcopy(stored_type.definition)
        del definition['requirements'][0]['to_minute']
        stored_type.definition = definition
        shift = db.get(m.Shift, sid)
        snapshot = copy.deepcopy(shift.type_snapshot)
        del snapshot['definition']['requirements'][0]['to_minute']
        shift.type_snapshot = snapshot

    data = h.snapshot(workspace)
    shift = data['shifts'][0]
    for operational in (False, True):
        validation = evaluate({**data, 'operational': operational})
        gap, = [f for f in validation['findings'] if f['code'] == 'COVERAGE_GAP']
        assert aware(gap['start']) == aware(shift['start'])
        assert aware(gap['end']) == aware(shift['end'])
        assert gap['detail'] == {'required': 1, 'available': 0}
    checked = h.command('coordinator', 'workspace.validate', {'reason': 'Check imported requirement'},
                        workspace['id'], workspace['revision'])
    assert any(f['code'] == 'COVERAGE_GAP' for f in checked['validation']['findings'])
    proof, = direct_coverage_shortages({**data, 'people': {}}, [], {sid})
    assert aware(proof['end']) == aware(shift['end'])
    assert proof['required'] == 1 and proof['available_upper_bound'] == 0
    generated = generate(data, 5)
    assert generated['status'] == 'candidate_feasible', generated
    assert generated['validation']['valid']
    assert len(generated['additions']) == 1
    assert aware(generated['additions'][0]['participant']['end']) == aware(shift['end'])
    with transaction(h.app.state.engine) as db:
        assert 'to_minute' not in db.get(m.AssignmentType, typ['id']).definition['requirements'][0]
        assert 'to_minute' not in db.get(m.Shift, sid).type_snapshot['definition']['requirements'][0]
