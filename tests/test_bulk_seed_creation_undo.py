"""Creation Undo preserves immutable seeded rule references."""
from copy import deepcopy

import pytest
from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services.scoring_defaults import line_id
from .test_bulk import REASON, preview
from .test_bulk_single_commands import undo as undo_command


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('bulk', [False, True])
def test_creation_undo_preserves_immutable_seed_references_and_valid_fair_share(request, database, bulk):
    h = request.getfixturevalue(database)
    value = {'name': 'Synthetic referenced duty', 'starts_on': '2020-01-01',
             'definition': {}}
    if bulk:
        created = h.command('admin', 'bulk.apply', {'item_kind': 'catalogue.create',
            'common': {'catalogue': 'assignment_types'}, 'items': [{'payload': {'value': value}}], 'reason': REASON})
        duty, undo = created['items'][0]['record'], created['undo']
    else:
        duty = h.configure('assignment_types', value)['record']
        undo = {'kind': 'catalogue.undo_create', 'target_id': duty['id'], 'expected_revision': duty['revision'],
                'payload': {'catalogue': 'assignment_types', 'reason': REASON}}
    fair = next(p for p in h.get('admin', '/api/policies?status=active')['policies']
                if p['definition']['rule'].get('id') == 'fair_share')
    seeded_id = m.uid()
    independent = {'id': seeded_id, 'where': {'match': 'all', 'conditions': [
        {'field': 'assignment_type_ids', 'op': 'any', 'values': [duty['id']]}]},
        'numbers': {'apply': {'value': None, 'level': 'medium'}},
        'settings': {'balance_goal': {'metric': 'hours', 'aim': 'equal_share', 'period': 'rota'}}}
    with transaction(h.app.state.engine, write=True) as db:
        seed = m.PolicySeedLine(id=seeded_id, department_id=h.seed['department_id'],
            policy_id=fair['id'], policy_revision=fair['revision'], line=deepcopy(independent))
        db.add(seed)
        db.flush()
        retained = record(seed)
    if bulk:
        checked = preview(h, 'admin', undo['payload'])
        assert checked['counts']['problem'] == 1
        assert checked['items'][0]['issues'][0]['code'] == 'INTEGRITY_CONFLICT'
    refused = undo_command(h, 'admin', undo, status=409)
    assert refused['error']['code'] == 'INTEGRITY_CONFLICT'
    with transaction(h.app.state.engine) as db:
        assert record(db.get(m.AssignmentType, duty['id'])) == duty
        assert record(db.get(m.PolicySeedLine, seeded_id)) == retained
    effective = next(p for p in h.get('admin', '/api/policies?status=active')['policies'] if p['id'] == fair['id'])
    assert seeded_id in {line['id'] for line in effective['definition']['rule']['lines']}
    proposed = h.command('admin', 'rule.propose', {'catalogue_id': 'fair_share', 'catalogue_version': 2,
        'expected_policy_id': effective['id'], 'expected_revision': effective['revision'],
        'expected_line_ids': [line['id'] for line in effective['definition']['rule']['lines']],
        'lines': effective['definition']['rule']['lines'], 'reason': REASON})['record']
    assert proposed['status'] == 'proposed'
    assert seeded_id in {line['id'] for line in proposed['definition']['rule']['lines']}


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('bulk', [False, True])
def test_new_leadership_cannot_be_activated_by_direct_or_bulk_type_creation(request, database, bulk):
    h = request.getfixturevalue(database)
    value = {'name': 'Unapproved clinical leadership', 'starts_on': '2020-01-01',
             'definition': {'lead': {'minimum': 1, 'maximum': 1}}}
    if bulk:
        checked = preview(h, 'admin', {'item_kind': 'catalogue.create',
            'common': {'catalogue': 'assignment_types'}, 'items': [{'payload': {'value': value}}], 'reason': REASON})
        assert checked['counts']['problem'] == 1
        assert checked['items'][0]['issues'][0]['code'] == 'USE_RULE_CATALOGUE'
    else:
        refused = h.configure('assignment_types', value, status=422)
        assert refused['error']['code'] == 'USE_RULE_CATALOGUE'
