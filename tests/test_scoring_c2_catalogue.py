"""Legacy lead caps remain evidence; present-day edits use independently approved Rules."""
from copy import deepcopy

import pytest
from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from packages.contracts.schemas import TypeDefinition
from tests.legacy_lead import historical_type


@pytest.mark.parametrize('purpose', ['staffed', 'protected'])
@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_legacy_lead_cannot_be_disabled_through_the_type_editor(request, database, purpose):
    h = request.getfixturevalue(database)
    created = historical_type(h, {'name': 'Legacy capped duty', 'starts_on': '2020-01-01',
        'definition': {'lead': {'most_turns': 2, 'most_turns_per': 'week'}}})
    definition = {**created['definition'], 'purpose': purpose, 'lead': None}
    if purpose == 'protected':
        definition['requirements'] = []
    refusal = h.configure('assignment_types', {'name': created['name'], 'starts_on': created['starts_on'],
        'definition': definition}, created['id'], created['revision'], status=422)
    assert refusal['error']['code'] == 'USE_RULE_CATALOGUE'
    with transaction(h.app.state.engine) as db:
        assert record(db.get(m.AssignmentType, created['id'])) == created


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_old_undo_payload_cannot_clear_imported_leadership(request, database):
    h = request.getfixturevalue(database)
    created = historical_type(h, {'name': 'Imported capped duty', 'starts_on': '2020-01-01',
        'definition': {'lead': {'most_turns': 2, 'most_turns_per': 'week'}}})
    payload = {'name': created['name'], 'starts_on': created['starts_on'], 'definition': {'lead': None}}
    refusal = h.command('admin', 'catalogue.update', {'catalogue': 'assignment_types', 'value': payload,
        'reason': 'Undo'}, created['id'], created['revision'], status=422)
    assert refusal['error']['code'] == 'USE_RULE_CATALOGUE'
    payload['definition'] = created['definition']
    payload['name'] = 'Metadata edit preserving leadership'
    updated = h.configure('assignment_types', payload, created['id'], created['revision'])['record']
    assert updated['definition'] == created['definition']


@pytest.mark.parametrize('changed', [{'most_turns': 3}, {'most_turns': None}, {'most_turns_per': 'month'}])
def test_retained_lead_still_rejects_legacy_cap_changes(h, changed):
    created = historical_type(h, {'name': 'Retained capped duty', 'starts_on': '2020-01-01',
        'definition': {'lead': {'most_turns': 2, 'most_turns_per': 'week'}}})
    definition = deepcopy(created['definition'])
    definition['lead'].update(changed)
    refusal = h.configure('assignment_types', {'name': created['name'], 'starts_on': created['starts_on'],
        'definition': definition}, created['id'], created['revision'], status=422)
    assert refusal['error']['code'] == 'USE_RULE_CATALOGUE'
# Include this file in hosted supervision and team-lead qualification.
