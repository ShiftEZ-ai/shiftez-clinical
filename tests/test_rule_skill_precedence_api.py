"""Person Rules and editor previews retain duty context without needing assignments."""
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import PolicyDefinition


def scoped_policy(h, metric, inherit, scoped=True):
    person, skill = h.seed['people'][0], h.seed['capability']
    clinic_a, clinic_b = h.seed['services']
    where = {'conditions': [{'field': 'service_ids', 'values': [clinic_b]}]}

    def line(amount, level, who=None):
        return {'id': str(uuid4()), 'who': who, 'where': where,
                'numbers': {'max': {'value': amount, 'level': level}},
                'settings': ({'block_quota': {'metric': 'duties', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}}
                             if metric == 'quota' else {'max_duties': {'window': 'week'}})}

    lines = [line(1, 'mandatory'), line(2, 'high',
             {'conditions': [{'field': 'capability_ids', 'values': [skill]}]})]
    if inherit:
        lines.append(line(3, None, {'conditions': [{'field': 'person_ids', 'values': [person]}]}))
    definition = PolicyDefinition.model_validate({'rule': {'kind': 'catalogue', 'catalogue_version': 2,
        'id': 'workload_limit', 'lines': lines}}).model_dump(mode='json')
    with transaction(h.app.state.engine, write=True) as db:
        for grant in db.scalars(select(m.CapabilityGrant).where(m.CapabilityGrant.person_id == person,
                m.CapabilityGrant.capability_id == skill)):
            grant.service_id = clinic_a if scoped else None
        policy = m.Policy(department_id=h.seed['department_id'], name='Clinic quota context', kind='catalogue',
            status='active', definition=definition, owner='Synthetic owner', source='Synthetic fixture',
            starts_on=date(2020, 1, 1), created_by=h.seed['identities']['coordinator'],
            approved_by=h.seed['identities']['approver'])
        db.add(policy)
        db.flush()
        policy_id = policy.id
    return person, policy_id, lines, definition


@pytest.mark.parametrize('metric,inherit,scoped', [
    ('quota', False, True), ('quota', True, True),
    ('week', False, True), ('week', True, True), ('week', False, False)])
def test_person_rules_and_previews_keep_clinic_specific_skill_context(h, monkeypatch, metric, inherit, scoped):
    monkeypatch.setattr('apps.api.services.independence.today', lambda db, department: date(2026, 10, 5))
    person, policy_id, lines, definition = scoped_policy(h, metric, inherit, scoped)
    result = h.get('coordinator', f'/api/policies/applies?person_id={person}&day=2026-10-05')
    applies = [row for row in result['rules'] if row['policy_id'] == policy_id]
    expected = ({('mandatory', 1, lines[0]['id']), ('advisory', 2, lines[1]['id'])} if not scoped else
                {('mandatory', 3 if inherit else 1, lines[-1 if inherit else 0]['id'])})
    # A more specific advisory line cannot displace the mandatory everyone line.
    assert {(row['severity'], row['rule']['amount' if metric == 'quota' else 'maximum'], row['own_line']['id'])
            for row in applies} == expected
    assert len(applies) == len(expected)
    # Both reads use the same complete people/catalogue projection, with no rota or work needed.
    detail = h.get('coordinator', f'/api/rules/catalogue/workload_limit/detail?version=2&day=2026-10-05')
    assert detail['entry']['rule']['id'] == policy_id and detail['check']['issues'] == []
    checked = h.client('coordinator').post('/api/rules/catalogue/workload_limit/check',
        json={'catalogue_version': 2, 'lines': lines})
    assert checked.status_code == 200 and checked.json()['issues'] == []
    preview = h.client('coordinator').post('/api/policies/check', json={'definition': definition})
    assert preview.status_code == 200 and preview.json()['compatible']
    if inherit:
        own = lines[-1]['id']
        # A synthetic preview carries its own source pool; an inherited line cannot silently lose its people.
        for reach in (checked.json()['reach_details'], preview.json()['reach_details']):
            [own_reach] = [row for row in reach['lines'] if row['line_id'] == own
                           and any(entry['person_id'] == person for entry in row['people'])]
            assert own_reach['number']['value'] == 3
        overlaps = checked.json()['line_overlaps'][own]
        assert any(row['people'] == 1 and row['winner'] == own for row in overlaps)


def test_person_rules_share_one_context_budget_with_inherited_quota_levels(h, monkeypatch):
    person, _, _, _ = scoped_policy(h, 'quota', True)
    monkeypatch.setattr('packages.domain.quotas.MAX_SUBJECT_CONTEXTS', 1)
    result = h.get('coordinator', f'/api/policies/applies?person_id={person}&day=2026-10-05', status=422)
    assert result['error']['code'] == 'VALUE_INVALID'
    assert 'narrower scope or separate reconciliation' in result['error']['message']
