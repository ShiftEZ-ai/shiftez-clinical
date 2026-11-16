"""Skill reach previews use the same coherent clinic context as actual duty checks."""
from datetime import date

import pytest
from sqlalchemy import update

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.rule_map import reach, rule_map
from packages.domain.time import aware
from packages.domain.validation import policy_applies
from .test_relative_filter_witnesses import CLINICS, SKILL, snapshot
from .test_rule_conditions import line, tree
from .test_rule_map import HOURS, U, entry, rule
from .test_rule_templates import propose


@pytest.mark.parametrize('grant', ['clinic', 'global', 'foreign', 'none'])
@pytest.mark.parametrize('op', ['empty', 'not_empty', 'none', 'is_not'])
@pytest.mark.parametrize('clinic', [0, 1])
def test_skill_reach_previews_match_the_actual_clinic(grant, op, clinic):
    data, person, a, b = snapshot(grant)
    condition = {'field': 'capability_ids', 'op': op}
    if op == 'is_not':
        condition['value'] = {'kind': 'absolute', 'value': [SKILL]}
    elif op == 'none':
        condition['values'] = [SKILL]
    scope = {'conditions': tree(condition, line('service_ids', CLINICS[clinic]))}
    policy = rule(data, 'Clinic skill reach', HOURS, scope)
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    shift = {'id': U('reach-duty'), 'assignment_type_id': 'T', 'location_id': 'room',
             'service_ids': [CLINICS[clinic]], 'start': start, 'end': end,
             'participants': [{'person_id': person, 'start': start, 'end': end}]}
    data.update(policies=[policy], shifts=[shift], focus_ids=[shift['id']])
    skilled = grant == 'global' or grant == 'clinic' and clinic == 0
    expected = skilled if op == 'not_empty' else not skilled
    assert policy_applies(data, policy, person, shift, aware(start), aware(end)) is expected
    mapped = entry(rule_map(data), policy['id'])
    assert (person in mapped['people'] and 0 not in mapped['not_here'].get(person, ())) is expected
    summary = reach(data, [policy], a, b)
    detailed = reach(data, [policy], a, b, details=True)
    assert summary == detailed['reach'] == {policy['id']: int(expected)}
    [found] = detailed['details'][policy['id']]['lines']
    assert [row['person_id'] for row in found['people']] == ([person] if expected else [])
    if expected:
        assert found['people'][0]['matched_paths'] == (['0.0', '0.1'] if op == 'not_empty' else ['0.1'])


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('op,clinic,expected', [
    ('empty', 0, False), ('empty', 1, True), ('not_empty', 0, True), ('not_empty', 1, False),
])
def test_saved_and_draft_api_reach_use_the_clinic_witness(request, database, op, clinic, expected):
    h = request.getfixturevalue(database)
    person, department = h.seed['people'][0], h.seed['department_id']
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(update(m.CapabilityGrant).where(m.CapabilityGrant.person_id == person).values(revoked=True))
        skill = m.Capability(department_id=department, name='Synthetic clinic skill', starts_on=date(2020, 1, 1))
        db.add(skill)
        db.flush()
        db.add(m.CapabilityGrant(department_id=department, person_id=person, capability_id=skill.id,
            service_id=h.seed['services'][0], granted_by=h.seed['identities']['credentialer'],
            evidence='Synthetic clinic-scoped qualification', starts_on=date(2020, 1, 1)))
    scope = {'conditions': tree(line('person_ids', person), {'field': 'capability_ids', 'op': op},
                                line('service_ids', h.seed['services'][clinic]))}
    policy = propose(h, HOURS, scope, name='Synthetic clinic reach')
    endpoint = f"/api/policies/reach?day=2026-10-04&policy_id={policy['id']}"
    assert h.get('coordinator', endpoint)['reach'] == {policy['id']: int(expected)}
    detailed = h.get('coordinator', endpoint + '&details=true')
    assert detailed['reach'] == {policy['id']: int(expected)}
    [found] = detailed['details'][policy['id']]['lines']
    assert [row['person_id'] for row in found['people']] == ([person] if expected else [])
    preview = h.client('coordinator').post('/api/policies/check', json={'definition': policy['definition']})
    assert preview.status_code == 200, preview.text
    [draft] = preview.json()['reach_details']['lines']
    assert [row['person_id'] for row in draft['people']] == ([person] if expected else [])
