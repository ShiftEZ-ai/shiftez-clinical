"""Rules reads stay bounded by relevant people and retain rules for different clinic contexts."""
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import PolicyDefinition
from packages.domain import conditions
from .test_rule_skill_precedence_api import scoped_policy
from .test_rule_conditions import data as condition_data, OCT


@pytest.mark.parametrize('scope,expected', [
    ({'person_ids': ['q']}, False),
    ({'conditions': {'conditions': [{'field': 'person_ids', 'op': 'none', 'values': ['p']}]}}, False),
    ({}, True),
    ({'conditions': {'match': 'any', 'conditions': [
        {'field': 'person_ids', 'values': ['q']}, {'field': 'capability_ids', 'values': ['S']}]}}, True),
])
def test_impossible_person_selection_uses_no_duty_context_budget(monkeypatch, scope, expected):
    contexts = 0
    original = conditions._scope_duty_contexts

    def counted(*args, **kwargs):
        nonlocal contexts
        for interval in original(*args, **kwargs):
            contexts += 1
            yield interval

    monkeypatch.setattr(conditions, '_scope_duty_contexts', counted)
    assert conditions.scope_has_duty_witness(condition_data(), scope, 'p', *OCT, 'd', budget=[1]) is expected
    assert contexts == int(expected)


def test_person_rules_skip_other_peoples_inherited_lines_at_department_scale(h, monkeypatch):
    target, skill, department = h.seed['people'][0], h.seed['capability'], h.seed['department_id']
    with transaction(h.app.state.engine, write=True) as db:
        extra_people = [m.Person(organisation_id=h.seed['organisation_id'], display_name=f'Synthetic clinician {n}')
                        for n in range(147)]
        extra_services = [m.Service(department_id=department, name=f'Synthetic clinic {n}',
                          starts_on=date(2020, 1, 1)) for n in range(7)]
        db.add_all([*extra_people, *extra_services])
        db.flush()
        people = [*h.seed['people'], *(p.id for p in extra_people)]
        clinics = [*h.seed['services'], *(service.id for service in extra_services)]
        db.add_all(m.Membership(department_id=department, person_id=p.id, category_id=h.seed['category'],
                               starts_on=date(2020, 1, 1)) for p in extra_people)
        for typ in db.scalars(select(m.AssignmentType).where(m.AssignmentType.department_id == department)):
            typ.definition = {**typ.definition, 'allow_shared_services': True,
                              'shared_duty_reason': 'Reviewed synthetic shared duty'}
        for grant in db.scalars(select(m.CapabilityGrant).where(m.CapabilityGrant.person_id == target,
                                m.CapabilityGrant.capability_id == skill)):
            grant.service_id = clinics[0]

        def line(amount, level, who=None):
            return {'id': str(uuid4()), 'who': who,
                    'where': {'conditions': [{'field': 'service_ids', 'values': clinics}]},
                    'numbers': {'max': {'value': amount, 'level': level}},
                    'settings': {'block_quota': {'metric': 'duties', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}}}

        lines = [line(30, 'mandatory'), line(40, 'high',
                 {'conditions': [{'field': 'capability_ids', 'values': [skill]}]})]
        for person in [*(person for person in people if person != target), target]:
            lines.append(line(35, None, {'conditions': [{'field': 'person_ids', 'values': [person]}]}))
        definition = PolicyDefinition.model_validate({'rule': {'kind': 'catalogue', 'catalogue_version': 2,
            'id': 'workload_limit', 'lines': lines}}).model_dump(mode='json')
        policy = m.Policy(department_id=department, name='Department quota context', kind='catalogue',
            status='active', definition=definition, owner='Synthetic owner', source='Synthetic fixture',
            starts_on=date(2020, 1, 1), created_by=h.seed['identities']['coordinator'],
            approved_by=h.seed['identities']['approver'])
        db.add(policy)
        db.flush()
        policy_id = policy.id
    assert len(people) == 150 and len(clinics) == 9
    contexts = 0
    original = conditions._scope_duty_contexts

    def counted(*args, **kwargs):
        nonlocal contexts
        for interval in original(*args, **kwargs):
            contexts += interval[2] is not None
            yield interval

    monkeypatch.setattr(conditions, '_scope_duty_contexts', counted)
    response = h.client('coordinator').get(f'/api/policies/applies?person_id={target}&day=2026-10-05')
    print(f'Person Rules: 150 people, 152 quota lines, 9 shared clinics, {contexts} virtual contexts, HTTP {response.status_code}')
    assert response.status_code == 200, response.text
    [applies] = [row for row in response.json()['rules'] if row['policy_id'] == policy_id]
    assert applies['own_line']['id'] == lines[-1]['id']
    assert applies['rule']['amount'] == 35 and applies['severity'] == 'mandatory'
    # Work for another person's line cannot consume this person's bounded context search.
    assert contexts < 5000


@pytest.mark.parametrize('metric', ['week', 'quota'])
@pytest.mark.parametrize('explicit_none', [False, True])
def test_catalogue_held_reach_keeps_rules_for_each_matching_clinic_context(h, metric, explicit_none):
    person, policy_id, lines, definition = scoped_policy(h, metric, False)
    if explicit_none:
        lines.append({**lines[0], 'id': str(uuid4()),
                      'who': {'conditions': [{'field': 'person_ids', 'values': [person]}]},
                      'numbers': {'max': {'value': None, 'level': 'mandatory'}}})
    for line in lines:
        line['where'] = None
    definition['rule']['lines'] = lines
    with transaction(h.app.state.engine, write=True) as db:
        for grant in db.scalars(select(m.CapabilityGrant).where(m.CapabilityGrant.capability_id == h.seed['capability'])):
            if grant.person_id != person:
                grant.revoked = True
        db.get(m.Policy, policy_id).definition = PolicyDefinition.model_validate(definition).model_dump(mode='json')
    page = h.get('coordinator', '/api/rules/catalogue?version=2&day=2026-10-05')
    entry = next(row for row in page['entries'] if row['id'] == 'workload_limit')
    assert entry['reach'][lines[0]['id']] == len(h.seed['people']) - int(explicit_none)
    # The mandatory None displaces the mandatory default, so the advisory skill line still applies.
    assert entry['reach'][lines[1]['id']] == 1
    if explicit_none:
        # An authored None-only line has reach even though it emits no numeric obligation.
        assert entry['reach'][lines[-1]['id']] == 1
    assert entry['people'] == len(h.seed['people'])
    applies = h.get('coordinator', f'/api/policies/applies?person_id={person}&day=2026-10-05')
    mine = [row for row in applies['rules'] if row['policy_id'] == policy_id]
    assert {row['own_line']['id'] for row in mine} == (
        {lines[1]['id']} if explicit_none else {line['id'] for line in lines})


@pytest.mark.parametrize('case', ['partial', 'equal_tier', 'safety'])
def test_catalogue_held_reach_preserves_split_numbers_equal_tiers_and_safety(h, case):
    person, policy_id, lines, definition = scoped_policy(h, 'quota' if case == 'partial' else 'week', False, False)
    for line in lines:
        line['where'] = None
    if case == 'partial':
        lines[0]['numbers'] = {'min': {'value': 1, 'level': 'mandatory'}, 'max': {'value': 3, 'level': 'mandatory'}}
        lines[1]['numbers']['max']['value'] = 4
    elif case == 'equal_tier':
        lines[0]['who'] = {'conditions': [{'field': 'category_ids', 'values': [h.seed['category']]}]}
    else:
        definition['rule']['id'] = 'people_per_session'
        for line, minimum in zip(lines, (2, 1)):
            line['settings'] = {}
            line['numbers'] = {'min': {'value': minimum, 'level': 'mandatory'}}
    definition['rule']['lines'] = lines
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Policy, policy_id).definition = PolicyDefinition.model_validate(definition).model_dump(mode='json')
    page = h.get('coordinator', '/api/rules/catalogue?version=2&day=2026-10-05')
    entry = next(row for row in page['entries'] if row['rule'] and row['rule']['id'] == policy_id)
    assert entry['reach'][lines[0]['id']] >= 1 and entry['reach'][lines[1]['id']] >= 1
    if case != 'equal_tier':
        assert entry['reach'][lines[0]['id']] == len(h.seed['people'])
    applies = h.get('coordinator', f'/api/policies/applies?person_id={person}&day=2026-10-05')
    mine = [row for row in applies['rules'] if row['policy_id'] == policy_id]
    assert {row['own_line']['id'] for row in mine} == {line['id'] for line in lines}
