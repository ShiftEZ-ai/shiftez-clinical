"""Canonical action kinds from the universal form retain their registry defaults."""
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .test_bulk import preview


@pytest.mark.parametrize('source', ['selection', 'items', 'items_without_common'])
@pytest.mark.parametrize('nested', [False, True])
def test_add_to_programme_accepts_the_real_form_action_payload_and_undo(h, source, nested):
    person = h.seed['people'][0]
    programme = h.configure('programmes', {'name': 'Synthetic bulk programme',
                                          'starts_on': '2026-01-01'})['record']
    fields = {'programme_id': programme['id'], 'starts_on': '2027-01-01', 'ends_on': '2027-04-01'}
    assert s.BULK_COLLECTIONS['people']['actions']['programme']['payload'] == {'catalogue': 'programme_memberships'}
    body = {'changes': [{'item_kind': 'catalogue.create', 'payload': {'value': fields} if nested else fields}],
            'reason': 'Add these colleagues to the programme'}
    if source == 'selection':
        body['selection'] = {'collection': 'people', 'include_ids': [person]}
    else:
        body['items'] = [{'target_id': person, 'expected_revision': 1}]
        if source == 'items':
            body['common'] = {'catalogue': 'people'}
    check = preview(h, 'coordinator', body)
    assert check['items'][0]['result'] == 'ready', check
    with transaction(h.app.state.engine) as db:
        assert not list(db.scalars(select(m.ProgrammeMembership)))
    response = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())},
        json={'kind': 'bulk.apply', 'payload': body, 'expected_fingerprint': check['source_fingerprint']})
    assert response.status_code == 200, response.text
    reply = response.json()
    membership = reply['items'][0]['results'][0]['record']
    assert {key: membership[key] for key in fields} == fields
    assert membership['person_id'] == person
    with transaction(h.app.state.engine) as db:
        assert db.get(m.ProgrammeMembership, membership['id']).programme_id == programme['id']
    h.command('coordinator', reply['undo']['kind'], reply['undo']['payload'])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.ProgrammeMembership, membership['id']) is None


def test_add_to_programme_retains_the_catalogue_handlers_validation(h):
    programme = h.configure('programmes', {'name': 'Synthetic bounded programme',
                                          'starts_on': '2026-01-01'})['record']
    check = preview(h, 'coordinator', {'selection': {'collection': 'people',
        'include_ids': [h.seed['people'][0]]}, 'changes': [{'item_kind': 'catalogue.create', 'payload': {
            'programme_id': programme['id'], 'starts_on': '2027-04-01', 'ends_on': '2027-01-01'}}],
        'reason': 'Validate the programme membership dates'})
    assert check['items'][0]['result'] == 'problem'
    assert check['items'][0]['issues'][0]['message'] == 'End must follow start'
