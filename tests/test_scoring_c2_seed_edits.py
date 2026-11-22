"""Seeded Fair share lines use the ordinary independently approved editing path."""
from tests.legacy_lead import historical_type, set_historical_lead

from copy import deepcopy
import importlib
import uuid

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services.scoring_defaults import line_id, seed
from packages.contracts.schemas import TypeDefinition
from packages.domain.conditions import active_rules
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate


@pytest.mark.parametrize('deleted', ['workload', 'lead'])
def test_deleting_a_seeded_line_sticks_through_approved_revisions(h, deleted):
    original_line = {'id': str(uuid.uuid4()), 'numbers': {'apply': {'level': 'medium'}},
        'settings': {'balance_goal': {'metric': 'hours', 'aim': 'equal_share', 'period': 'rota'}}}
    existing = next(p for p in h.get('admin', '/api/policies?status=active')['policies']
                    if p['definition']['rule'].get('id') == 'fair_share')
    proposal = h.command('admin', 'rule.propose', {'catalogue_id': 'fair_share', 'catalogue_version': 1,
        'expected_policy_id': existing['id'], 'expected_revision': existing['revision'],
        'lines': [original_line], 'reason': 'Existing approved Fair share goal'})['record']
    original = h.command('approver', 'policy.activate', {'reason': 'Independent original review'},
        proposal['id'], proposal['revision'])['record']
    with transaction(h.app.state.engine, write=True) as db:
        typ = db.get(m.AssignmentType, h.seed['type'])
        typ.definition = TypeDefinition.model_validate({**typ.definition,
            'lead': {'minimum': 1, 'maximum': 1}}).model_dump(mode='json')
    with h.app.state.engine.begin() as connection:
        importlib.import_module('migrations.versions.0109_fair_share_dials').backfill(connection)
    workspace = h.workspace()
    for day in (4, 5):
        start, end = f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00'
        workspace = h.shift(workspace, start=start, end=end, people=[{
            'person_id': h.seed['people'][0], 'start': start, 'end': end, 'team_lead': True}])['workspace']
    seeded = next(p for p in h.get('admin', '/api/policies?status=active')['policies'] if p['id'] == original['id'])
    deleted_id = line_id(h.seed['department_id'], 'workload' if deleted == 'workload' else 'lead:' + h.seed['type'])
    assert deleted_id in {line['id'] for line in seeded['definition']['rule']['lines']}
    # The same pre-edit fixture, with just this seed removed, is the expected score.
    reference = deepcopy(h.snapshot(workspace))
    reference['policy_seed_lines'] = [r for r in reference['policy_seed_lines'] if r['id'] != deleted_id]
    for policy in reference['policies']:
        rule = policy['definition']['rule']
        if rule['kind'] == 'catalogue':
            rule['lines'] = [line for line in rule['lines'] if line['id'] != deleted_id]
    expected = schedule_score(reference, evaluate(reference))
    current = seeded
    for _ in range(2):
        kept = [line for line in current['definition']['rule']['lines'] if line['id'] != deleted_id]
        proposal = h.command('admin', 'rule.propose', {'catalogue_id': 'fair_share', 'catalogue_version': 1,
            'expected_policy_id': current['id'], 'expected_revision': current['revision'], 'lines': kept,
            'expected_line_ids': [line['id'] for line in current['definition']['rule']['lines']],
            'reason': 'Keep the approved removal of a seeded preference'})['record']
        current = h.command('approver', 'policy.activate', {'reason': 'Independent removal review'},
            proposal['id'], proposal['revision'])['record']
        workspace = h.refresh_draft_baseline(workspace)
        data = h.snapshot(workspace)
        current = next(p for p in h.get('admin', '/api/policies?status=active')['policies'] if p['id'] == current['id'])
        assert {line['id'] for line in current['definition']['rule']['lines']} == {line['id'] for line in kept}
        metric = 'workload' if deleted == 'workload' else 'lead_turns'
        assert not [p for p in active_rules(data) if p['definition']['rule'].get('metric') == metric]
        actual = schedule_score(data, evaluate(data))
        assert actual['score'] == expected['score']
        assert actual['components'] == expected['components']


@pytest.mark.parametrize('representation', ['appended', 'embedded', 'standalone'])
def test_deleted_lead_default_is_not_reseeded_when_duty_lead_is_reenabled(h, representation):
    [fair] = h.get('admin', '/api/policies?status=active')['policies']
    if representation == 'embedded':
        h.command('approver', 'policy.retire', {'reason': 'Retire original Fair share default'},
            fair['id'], fair['revision'])
    elif representation == 'standalone':
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Policy, fair['id'])
            row.definition = {**row.definition, 'rule': {**row.definition['rule'], 'on': False}}
    value = {'name': 'Led duty', 'starts_on': '2020-01-01',
             'definition': {'lead': {'minimum': 1, 'maximum': 1}}}
    duty = historical_type(h, value)
    deleted_id = line_id(h.seed['department_id'], 'lead:' + duty['id'])
    policies = h.get('admin', '/api/policies?status=active')['policies']
    if representation == 'standalone':
        lead = next(p for p in policies if p['id'] == deleted_id)
        h.command('approver', 'policy.retire', {'reason': 'Approve removal of the lead preference'},
            lead['id'], lead['revision'])
    else:
        current = next(p for p in policies if p['definition']['rule'].get('id') == 'fair_share')
        assert deleted_id in {line['id'] for line in current['definition']['rule']['lines']}
        proposal = h.command('admin', 'rule.propose', {'catalogue_id': 'fair_share', 'catalogue_version': 2,
            'expected_policy_id': current['id'], 'expected_revision': current['revision'],
            'expected_line_ids': [line['id'] for line in current['definition']['rule']['lines']],
            'lines': [line for line in current['definition']['rule']['lines'] if line['id'] != deleted_id],
            'reason': 'Remove the seeded lead preference'})['record']
        h.command('approver', 'policy.activate', {'reason': 'Independent removal review'},
            proposal['id'], proposal['revision'])
    with transaction(h.app.state.engine) as db:
        seed = db.get(m.PolicySeedLine if representation == 'appended' else m.Policy,
                      deleted_id if representation != 'embedded' else current['id'])
        retained = record(seed)
    for lead in (None, {'minimum': 1, 'maximum': 1}):
        value['definition']['lead'] = lead
        duty = set_historical_lead(h, duty['id'], lead)
    policies = h.get('admin', '/api/policies?status=active')['policies']
    assert not [p for p in active_rules(policies) if p['definition']['rule'].get('metric') == 'lead_turns']
    with transaction(h.app.state.engine) as db:
        assert record(db.get(type(seed), retained['id'])) == retained


def test_unapproved_lead_line_does_not_suppress_first_duty_default(h):
    value = {'name': 'Future led duty', 'starts_on': '2020-01-01', 'definition': {}}
    duty = historical_type(h, value)
    seeded_id = line_id(h.seed['department_id'], 'lead:' + duty['id'])
    [fair] = h.get('admin', '/api/policies?status=active')['policies']
    proposed_line = {'id': seeded_id, 'numbers': {'apply': {'level': 'high'}},
        'settings': {'balance_goal': {'metric': 'lead_turns', 'aim': 'equal_share',
            'period': 'rota', 'assignment_type_ids': [duty['id']]}}}
    proposal = h.command('admin', 'rule.propose', {'catalogue_id': 'fair_share', 'catalogue_version': 2,
        'expected_policy_id': fair['id'], 'expected_revision': fair['revision'],
        'lines': [*fair['definition']['rule']['lines'], proposed_line],
        'reason': 'A proposed lead preference that has not been approved'})['record']
    value['definition']['lead'] = {'minimum': 1, 'maximum': 1}
    set_historical_lead(h, duty['id'], value['definition']['lead'])
    policies = h.get('admin', '/api/policies?status=active')['policies']
    [lead] = [p for p in active_rules(policies) if p['definition']['rule'].get('metric') == 'lead_turns']
    assert lead['definition']['rule']['assignment_type_ids'] == [duty['id']]
    with transaction(h.app.state.engine) as db:
        assert db.get(m.PolicySeedLine, seeded_id).policy_id == fair['id']
        assert db.get(m.Policy, proposal['id']).status == 'proposed'
    refused = h.command('approver', 'policy.activate', {'reason': 'Review the predicted seed line'},
        proposal['id'], proposal['revision'], status=409)
    assert refused['error']['code'] == 'STALE_REVISION'


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('stage', ['editor', 'pending'])
def test_new_lead_seed_refuses_stale_editor_and_pending_proposal_without_losing_defaults(request, database, stage):
    h = request.getfixturevalue(database)
    [opened] = h.get('admin', '/api/policies?status=active')['policies']
    lines = deepcopy(opened['definition']['rule']['lines'])
    lines[0]['numbers']['apply']['level'] = 'high'
    payload = {'catalogue_id': 'fair_share', 'catalogue_version': 2,
        'expected_policy_id': opened['id'], 'expected_revision': opened['revision'],
        'expected_line_ids': [line['id'] for line in opened['definition']['rule']['lines']],
        'lines': lines, 'reason': 'Edit the workload strength from the opened form'}
    if stage == 'pending':
        proposal = h.command('admin', 'rule.propose', payload)['record']
        assert proposal['definition']['seeded_line_ids'] == []
    duty = historical_type(h, {'name': 'New team-led duty', 'starts_on': '2020-01-01',
        'definition': {'lead': {'minimum': 1, 'maximum': 1}}})
    default_id = line_id(h.seed['department_id'], 'lead:' + duty['id'])
    with transaction(h.app.state.engine) as db:
        retained = record(db.get(m.PolicySeedLine, default_id))
    if stage == 'editor':
        refused = h.command('admin', 'rule.propose', payload, status=409)
        # Clients without a loaded-line baseline must also retain every current seed.
        refused_legacy = h.command('admin', 'rule.propose',
            {key: value for key, value in payload.items() if key != 'expected_line_ids'}, status=409)
        assert refused_legacy['error']['code'] == 'STALE_REVISION'
    else:
        refused = h.command('approver', 'policy.activate', {'reason': 'Independent stale review'},
            proposal['id'], proposal['revision'], status=409)
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Policy, proposal['id']).status == 'proposed'
        h.command('admin', 'policy.decline', {'reason': 'Reload after new defaults were seeded'},
            proposal['id'], proposal['revision'])
    assert refused['error']['code'] == 'STALE_REVISION'
    with transaction(h.app.state.engine, write=True) as db:
        seed(db, h.seed['department_id'])
        assert record(db.get(m.PolicySeedLine, default_id)) == retained
    [current] = h.get('admin', '/api/policies?status=active')['policies']
    assert (current['id'], current['revision']) == (opened['id'], opened['revision'])
    assert default_id in {line['id'] for line in current['definition']['rule']['lines']}
    empty_base = h.command('admin', 'rule.propose', {**payload, 'expected_line_ids': [],
        'lines': current['definition']['rule']['lines']}, status=409)
    assert empty_base['error']['code'] == 'STALE_REVISION'
    payload.update(expected_line_ids=[line['id'] for line in current['definition']['rule']['lines']],
        lines=[*lines, *[line for line in current['definition']['rule']['lines'] if line['id'] == default_id]])
    fresh = h.command('admin', 'rule.propose', payload)['record']
    assert fresh['definition']['seeded_line_ids'] == [default_id]
    h.command('approver', 'policy.activate', {'reason': 'Review refreshed defaults and workload edit'},
        fresh['id'], fresh['revision'])
    with transaction(h.app.state.engine, write=True) as db:
        seed(db, h.seed['department_id'])
        assert record(db.get(m.PolicySeedLine, default_id)) == retained
    [lead] = [p for p in active_rules(h.get('admin', '/api/policies?status=active')['policies'])
              if p['definition']['rule'].get('metric') == 'lead_turns']
    assert lead['definition']['rule']['assignment_type_ids'] == [duty['id']]


def test_policy_create_cannot_supply_the_server_seed_baseline(h):
    refused = h.command('admin', 'policy.create', {'name': 'Forged proposal baseline', 'owner': 'Department',
        'source': 'Synthetic fixture', 'starts_on': '2020-01-01', 'definition': {
            'rule': {'kind': 'min_rest', 'hours': 11}, 'seeded_line_ids': [str(uuid.uuid4())]}}, status=409)
    assert refused['error']['code'] == 'SOURCE_BINDING_IMMUTABLE'


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_retired_source_keeps_new_seed_in_pending_proposal_stale_guard(request, database):
    h = request.getfixturevalue(database)
    [opened] = h.get('admin', '/api/policies?status=active')['policies']
    proposal = h.command('admin', 'rule.propose', {'catalogue_id': 'fair_share', 'catalogue_version': 2,
        'expected_policy_id': opened['id'], 'expected_revision': opened['revision'],
        'expected_line_ids': [line['id'] for line in opened['definition']['rule']['lines']],
        'lines': opened['definition']['rule']['lines'], 'reason': 'A proposal before the new duty'})['record']
    duty = historical_type(h, {'name': 'New lead duty before retirement', 'starts_on': '2020-01-01',
        'definition': {'lead': {'minimum': 1, 'maximum': 1}}})
    default_id = line_id(h.seed['department_id'], 'lead:' + duty['id'])
    with transaction(h.app.state.engine) as db:
        retained = record(db.get(m.PolicySeedLine, default_id))
    retired = h.command('approver', 'policy.retire', {'reason': 'Retire source while a proposal is pending'},
        opened['id'], opened['revision'])['record']
    assert retired['revision'] > retained['policy_revision']
    assert default_id in {line['id'] for line in retired['definition']['rule']['lines']}
    refused = h.command('approver', 'policy.activate', {'reason': 'Attempt activation after source retirement'},
        proposal['id'], proposal['revision'], status=409)
    assert refused['error']['code'] == 'STALE_REVISION'
    with transaction(h.app.state.engine, write=True) as db:
        seed(db, h.seed['department_id'])
        assert record(db.get(m.PolicySeedLine, default_id)) == retained
        assert record(db.get(m.Policy, retired['id'])) == retired
        assert record(db.get(m.Policy, proposal['id'])) == proposal
    h.command('admin', 'policy.decline', {'reason': 'Reload the outdated proposal'},
        proposal['id'], proposal['revision'])
    restored = h.command('approver', 'policy.restore', {'reason': 'Restore the source with all its defaults'},
        retired['id'], retired['revision'])['record']
    fresh = h.command('admin', 'rule.propose', {'catalogue_id': 'fair_share', 'catalogue_version': 2,
        'expected_policy_id': restored['id'], 'expected_revision': restored['revision'],
        'expected_line_ids': [line['id'] for line in restored['definition']['rule']['lines']],
        'lines': restored['definition']['rule']['lines'], 'reason': 'Reloaded source after its lifecycle changes'})['record']
    assert fresh['definition']['seeded_line_ids'] == [default_id]
    h.command('approver', 'policy.activate', {'reason': 'Approve the refreshed source with its defaults'},
        fresh['id'], fresh['revision'])
    [current] = h.get('admin', '/api/policies?status=active')['policies']
    assert default_id in {line['id'] for line in current['definition']['rule']['lines']}
