import uuid

import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s
from packages.contracts.rule_catalogue import catalogue_entry


def line(**kw):
    return {'id': str(uuid.uuid4()), 'numbers': {'min': {'value': 1, 'level': 'mandatory'},
            'max': {'value': 1, 'level': 'mandatory'}}, **kw}


@pytest.mark.parametrize('version', [1, 2])
def test_leadership_authoring_contract(version):
    entry = catalogue_entry('team_leads', version)
    assert entry['authorable'] and entry['safety'] and entry['count_sides'] == []
    assert entry['line_settings'] == {'team_lead_requirement': ['continuity', 'count_scope']}
    assert entry['setting_labels']['count_scope'] == 'Count leaders'
    assert entry['setting_help']['count_scope'] == 'Count leaders per duty, or across these concurrent duties together.'
    rule = s.CatalogueRule(kind='catalogue', id='team_leads', catalogue_version=version, lines=[line()])
    assert rule.lines[0].numbers['min'].value == 1
    shared = s.CatalogueRule(kind='catalogue', id='team_leads', catalogue_version=version,
                             lines=[line(settings={'team_lead_requirement': {'count_scope': 'concurrent_duties'}})])
    assert shared.lines[0].settings['team_lead_requirement']['count_scope'] == 'concurrent_duties'
    for bad in [line(numbers={'min': {'value': 1, 'level': 'low'}}),
                line(numbers={'min': {'value': 2, 'level': 'mandatory'}, 'max': {'value': 1, 'level': 'mandatory'}}),
                line(who={'match': 'all', 'count': 'each', 'conditions': []})]:
        with pytest.raises(ValidationError):
            s.CatalogueRule(kind='catalogue', id='team_leads', catalogue_version=version, lines=[bad])


def test_leadership_is_safety_and_defaults_to_whole_duty():
    rule = s.TeamLeadRequirement(kind='team_lead_requirement')
    assert rule.minimum == 0 and rule.maximum is None and rule.continuity == 'whole_duty' and rule.count_scope == 'duty'
    assert s.safety_rule({'rule': rule.model_dump()})
    with pytest.raises(ValidationError):
        s.PolicyDefinition(rule=rule, exception_allowed=True)
    with pytest.raises(ValidationError):
        s.PolicyDefinition(rule=rule, severity='advisory')


def test_native_leadership_passes_through_later_department_conversion():
    from packages.domain.rule_catalogue_migration import plan_v2, after_snapshot
    ident, department = str(uuid.uuid4()), str(uuid.uuid4())
    policy = {'id': ident, 'revision': 2, 'department_id': department, 'name': 'Approved leadership',
              'kind': 'catalogue', 'status': 'active', 'active': True,
              'starts_on': '2026-10-01', 'ends_on': None,
              'definition': s.PolicyDefinition(rule=s.CatalogueRule(kind='catalogue', id='team_leads',
                  catalogue_version=2, lines=[line()])).model_dump(mode='json')}
    plan = plan_v2([policy], department_id=department)
    assert not plan['blocked'] and not plan['targets'] and not plan['bindings']
    receipts = [{'id': str(uuid.uuid4()), 'initial_policy_id': ident}]
    data = {'policies': [policy], 'type_lead_ownership': receipts, 'scheduling_facts': [], 'source_bindings': []}
    after = after_snapshot(data, plan)
    assert after['policies'] == [policy] and after['type_lead_ownership'] == receipts
