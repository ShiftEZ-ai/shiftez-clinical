import pytest

from packages.domain.solver_rules import definition_issues, capability_catalogue, SUPPORTED_RULES
from .governance_support import identity


def test_definition_checks_reject_precision_and_illegal_rule_combinations():
    assert not definition_issues({'rule': {'kind': 'min_rest', 'hours': 12}})
    issues = definition_issues({'severity': 'advisory', 'rule': {
        'kind': 'allocation_preference', 'priority': 1, 'balance_weight': 0.1234567}})
    assert issues[0]['field'] == 'definition.rule.balance_weight'
    assert definition_issues({'severity': 'advisory', 'rule': {
        'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 2}})
    assert definition_issues({'rule': {'kind': []}})
    catalogue = capability_catalogue()
    assert set(catalogue['rule_kinds']) == SUPPORTED_RULES
    assert 'team_lead_requirement' in catalogue['rule_kinds']
    assert definition_issues({'severity': 'advisory', 'rule': {'kind': 'team_lead_requirement', 'minimum': 1}})
    assert catalogue['definition_schema']['properties']['rule']['discriminator']


def test_unsaved_check_and_save_share_compatibility_and_permissions(h):
    client = h.client('coordinator')
    definition = {'severity': 'advisory', 'rule': {
        'kind': 'allocation_preference', 'priority': 1, 'balance_weight': 0.1234567}}
    response = client.post('/api/policies/check', json={'definition': definition})
    assert response.status_code == 200, response.text
    assert not response.json()['compatible']
    assert response.json()['feasibility'] == 'not_checked'
    result = h.command('coordinator', 'policy.create', {
        'name': 'Clinic preference', 'owner': 'FM coordinator',
        'source': 'Constructed rule check', 'starts_on': '2026-09-01',
        'definition': definition}, status=422)
    assert result['error']['code'] == 'SOLVER_RULE_INCOMPATIBLE'
    definition['rule']['balance_weight'] = 0.25
    assert client.post('/api/policies/check', json={'definition': definition}).json()['compatible']
    assert h.client('staff').post('/api/policies/check', json={'definition': definition}).status_code == 403
    assert h.client('staff').get('/api/policies/capabilities').status_code == 403
    assert client.get('/api/policies/capabilities').status_code == 200

@pytest.mark.parametrize('role', ['admin', 'coordinator', 'approver', 'credentialer', 'programme_lead', 'auditor', 'staff', 'operator'])
def test_capability_metadata_readers_do_not_gain_unsaved_policy_check_permission(h, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    client = h.client(role)
    readable = role not in ('staff', 'operator')
    response = client.get('/api/policies/capabilities')
    assert response.status_code == (200 if readable else 403), response.text
    if readable:
        expected = capability_catalogue()
        assert response.json()['rule_kinds'] == expected['rule_kinds']
        assert response.json()['definition_schema'] == expected['definition_schema']
    # Reading static schemas must not grant the separate unsaved-check operation.
    checked = client.post('/api/policies/check', json={'definition': {'rule': {'kind': 'min_rest', 'hours': 12}}})
    assert checked.status_code == (200 if role in ('admin', 'coordinator', 'approver') else 403), checked.text
    if checked.status_code == 200:
        assert checked.json()['compatible'] is True
