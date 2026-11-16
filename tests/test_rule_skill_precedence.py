"""Clinic-scoped skills affect a catalogue line only on matching duties."""
import copy
import uuid

import pytest

from packages.contracts.schemas import PolicyDefinition
from packages.domain.conditions import active_rules
from packages.domain.cp_sat import generate
from packages.domain.time import aware
from packages.domain.validation import evaluate, policy_applies


def _line(number, *, who=None, level='mandatory'):
    return {'id': str(uuid.uuid4()), 'numbers': {'max': {'value': number, 'level': level}},
            **({'who': who} if who else {})}


def _who(field, value):
    return {'match': 'all', 'conditions': [{'field': field, 'op': 'any', 'values': [value]}]}


def _fixture(h, version, service=1):
    person, skill = h.seed['people'][0], h.seed['capability']
    typ = h.configure('assignment_types', {'name': 'Scoped skill precedence', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    workspace = h.workspace()
    for start, end in (('08:00', '10:00'), ('13:00', '15:00')):
        workspace = h.shift(workspace, typ=typ, service=service, people=[],
            start=f'2026-10-04T{start}:00+03:00', end=f'2026-10-04T{end}:00+03:00')['workspace']
    data = h.snapshot(workspace)
    data['people'] = {person: data['people'][person]}
    for grant in data['grants']:
        if grant['person_id'] == person and grant['capability_id'] == skill:
            grant['service_id'] = h.seed['services'][0]
    lines = [_line(1), _line(2, who=_who('capability_ids', skill))]
    rule = {'kind': 'catalogue', 'catalogue_version': version,
            'id': 'workload_limit' if version == 2 else 'duties_per_week', 'lines': lines}
    policy = {'id': str(uuid.uuid4()), 'name': 'Scoped skill week limit', 'kind': 'catalogue',
              'department_id': h.seed['department_id'], 'status': 'active', 'active': True,
              'starts_on': '2020-01-01', 'ends_on': None, 'revision': 1,
              'definition': PolicyDefinition.model_validate({'rule': rule}).model_dump(mode='json')}
    data['policies'] = [policy]
    return data, policy, person, skill


@pytest.mark.parametrize('version', [1, 2])
def test_skill_line_does_not_remove_default_at_an_ungranted_clinic(h, version):
    data, _, person, _ = _fixture(h, version)
    data['policies'] = active_rules(data['policies'])
    shift = data['shifts'][0]
    a, b = aware(shift['start']), aware(shift['end'])
    # A skill in clinic A cannot remove the everyone obligation at clinic B.
    assert [(p['definition']['rule']['maximum'], policy_applies(data, p, person, shift, a, b))
            for p in data['policies']] == [(1, True), (2, False)]


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('service', [0, 1])
def test_validator_and_solver_enforce_the_matching_skill_or_default_limit(h, version, service):
    data, _, person, _ = _fixture(h, version, service)
    witness = copy.deepcopy(data)
    for shift in witness['shifts']:
        shift['participants'] = [{'id': str(uuid.uuid4()), 'shift_id': shift['id'], 'person_id': person,
            'start': shift['start'], 'end': shift['end'], 'participation': 'staff', 'overtime': False, 'revision': 1}]
    # Two duties are permitted only where the person's clinic-specific skill is held.
    allowed = service == 0
    validation = evaluate(witness)
    assert validation['valid'] is allowed, validation
    assert any(f['code'] == 'MAX_DUTIES' for f in validation['findings']) is not allowed
    result = generate(data, 3, feasibility_only=True)
    assert result['status'] == ('candidate_feasible' if allowed else 'requirements_conflict'), result
    if allowed:
        assert result['validation']['valid'], result


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('service_order', [(0, 1), (1, 0)])
def test_inherited_strength_keeps_the_current_clinic_in_both_caches(h, version, service_order):
    data, policy, person, skill = _fixture(h, version)
    policy['definition']['rule']['lines'] = [
        _line(1, level='low'), _line(2, who=_who('capability_ids', skill), level='high'),
        _line(3, who=_who('person_ids', person), level=None)]
    own = policy['definition']['rule']['lines'][-1]['id']
    data['policies'] = active_rules([policy])
    data['_line_index_cache'], data['_person_dates_cache'] = {}, {}
    inherited = [p for p in data['policies'] if p['template_line']['id'] == own]
    shift = data['shifts'][0]
    a, b = aware(shift['start']), aware(shift['end'])
    for service in service_order:
        # Reuse the same duty id too: a scope edit must not reuse the other clinic's result.
        shift['service_ids'] = [h.seed['services'][service]]
        strengths = [p['definition']['strength'] for p in inherited if policy_applies(data, p, person, shift, a, b)]
        assert strengths == ['high' if service == 0 else 'low']
