"""Undo and its inverse preserve both approved members of a replacement chain."""
from copy import deepcopy
from datetime import date

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from packages.contracts import schemas as s
from .test_bulk import preview
from .test_scheduling_facts_api import workload, decide, replace


def chain(h, length=2):
    rows = [decide(h, h.command('coordinator', 'person.workload.propose', workload(h))['fact'])['fact']]
    for amount in range(3, length + 2):
        rows.append(decide(h, replace(h, rows[-1], amount))['fact'])
    return rows


def state(h):
    with transaction(h.app.state.engine) as db:
        return {row.id: (row.status, row.revision) for row in db.scalars(select(m.SchedulingFact))}


def undo_fact(row):
    return {'item_kind': 'person.workload.undo', 'items': [{'target_id': row['id'],
        'expected_revision': row['revision']}], 'reason': 'Restore the approved predecessor'}


def bound_replacement(h, previous, amount):
    payload = {key: deepcopy(value) for key, value in previous['data'].items()
               if key in s.SchedulingFactInput.model_fields}
    payload.update(kind=previous['kind'], person_id=previous['person_id'], on=previous['on'],
                   replaces_id=previous['id'], expected_revision=previous['revision'],
                   reason='Replace the reviewed inherited personal amount')
    payload['line']['numbers']['max']['value'] = amount
    return decide(h, h.command('coordinator', 'person.workload.propose', payload)['fact'])['fact']


def inherited_chain(h, kind):
    if kind == 'native':
        return chain(h)
    person = h.seed['people'][0]
    definition = {'rule': {'kind': 'max_duties', 'maximum': 5, 'window': 'week'},
                  'scope': {'person_id': person}}
    source = h.command('coordinator', 'policy.create', {'name': 'Synthetic converted personal limit',
        'owner': 'Synthetic department', 'source': 'Reviewed inherited source fixture',
        'starts_on': '2020-01-01', 'definition': definition})['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent source review'},
              source['id'], source['revision'])
    from .test_rule_catalogue_v2_api import proposed
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'},
              conversion['id'], conversion['revision'])
    with transaction(h.app.state.engine) as db:
        previous = record(db.scalar(select(m.SchedulingFact).where(m.SchedulingFact.kind == 'workload',
                                                                  m.SchedulingFact.status == 'active')))
    assert previous['migration_proposal_id'] == conversion['id']
    if kind.endswith('default'):
        # Older conversions retained personal defaults; new conversions correctly refuse to create them.
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.SchedulingFact, previous['id'])
            line = deepcopy(row.data['line'])
            baseline = s.RuleLine.model_validate({key: value for key, value in line.items()
                if key not in {'source_default', 'source_bindings'}}).model_dump(mode='json')
            row.data = {**row.data, 'line': {**line, 'source_default': baseline}}
            previous = record(row)
        assert previous['data']['line']['source_default']
    if kind == 'native_default':
        previous = bound_replacement(h, previous, 6)
        assert previous['migration_proposal_id'] is None and previous['data']['line']['source_default']
    return previous, bound_replacement(h, previous, 7)


@pytest.mark.parametrize('kind', ['converted', 'converted_default', 'native_default'])
def test_paired_fact_inverse_roundtrips_inherited_predecessors_without_relaxing_standalone_guards(h, kind):
    previous, replacement = inherited_chain(h, kind)
    original_data = deepcopy(previous['data'])
    for _ in range(2):
        with transaction(h.app.state.engine) as db:
            replacement = record(db.get(m.SchedulingFact, replacement['id']))
        undone = h.command('approver', 'bulk.apply', undo_fact(replacement))
        with transaction(h.app.state.engine) as db:
            previous = record(db.get(m.SchedulingFact, previous['id']))
        assert previous['status'] == 'active' and previous['data'] == original_data
        refused = decide(h, previous, 'retire', status=409)
        assert refused['error']['code'] == ('CONVERSION_CHILD' if previous['migration_proposal_id'] else 'FACT_SOURCE_DEFAULT')
        with transaction(h.app.state.engine) as db:
            retired = record(db.get(m.SchedulingFact, replacement['id']))
        refused = decide(h, retired, 'undo', status=409)
        assert refused['error']['code'] == 'LATER_REPLACEMENT'
        assert len(undone['undo']['payload']['items']) == 1
        checked = preview(h, 'approver', undone['undo']['payload'])
        assert checked['items'][0]['result'] == 'ready', checked
        redone = h.command('approver', undone['undo']['kind'], undone['undo']['payload'])
        assert redone['items'][0]['record']['status'] == 'active'
        assert state(h)[previous['id']][0] == 'retired'


@pytest.mark.parametrize('length', [2, 3])
def test_redone_replacement_supports_fresh_undo_cycles_and_a_new_successor(h, length):
    rows = chain(h, length)
    replacement = rows[-1]
    for _ in range(3):
        with transaction(h.app.state.engine) as db:
            replacement = record(db.get(m.SchedulingFact, replacement['id']))
        undone = h.command('approver', 'bulk.apply', undo_fact(replacement))
        h.command('approver', undone['undo']['kind'], undone['undo']['payload'])
        with transaction(h.app.state.engine) as db:
            replacement = record(db.get(m.SchedulingFact, replacement['id']))
            predecessor = db.get(m.SchedulingFact, replacement['replaces_id'])
            assert replacement['replaces_revision'] + 1 == predecessor.revision
        assert replacement['status'] == 'active'
    successor = decide(h, replace(h, replacement, 7))['fact']
    undone = h.command('approver', 'bulk.apply', undo_fact(successor))
    h.command('approver', undone['undo']['kind'], undone['undo']['payload'])
    with transaction(h.app.state.engine) as db:
        successor = record(db.get(m.SchedulingFact, successor['id']))
    h.command('approver', 'bulk.apply', undo_fact(successor))
    assert state(h)[replacement['id']][0] == 'active'


@pytest.mark.parametrize('length', [2, 3])
def test_active_replacement_fact_bulk_undo_and_redo_roundtrip_the_whole_chain(h, length):
    rows = chain(h, length)
    initial = state(h)
    check = preview(h, 'approver', undo_fact(rows[-1]))
    assert check['items'][0]['result'] == 'ready', check
    assert state(h) == initial
    reply = h.command('approver', 'bulk.apply', undo_fact(rows[-1]))
    assert state(h)[rows[-1]['id']][0] == 'retired'
    assert state(h)[rows[-2]['id']][0] == 'active'
    assert [(item['item_kind'], item['target_id']) for item in reply['undo']['payload']['items']] == [
        ('person.workload.undo', rows[-1]['id'])]
    assert reply['undo']['payload']['items'][0]['payload']['predecessor_revision'] == state(h)[rows[-2]['id']][1]
    for turn in range(4):
        before = state(h)
        checked = preview(h, 'approver', reply['undo']['payload'])
        assert all(row['result'] == 'ready' for row in checked['items']), checked
        assert state(h) == before
        reply = h.command('approver', reply['undo']['kind'], reply['undo']['payload'])
        active = rows[-1 if turn % 2 == 0 else -2]['id']
        assert {key: status for key, (status, _) in state(h).items()} == {
            row['id']: 'active' if row['id'] == active else 'retired' for row in rows}


@pytest.mark.parametrize('changed', ['predecessor', 'replacement'])
@pytest.mark.parametrize('kind', ['native', 'converted', 'native_default'])
def test_replacement_fact_inverse_refuses_a_changed_member_atomically(h, changed, kind):
    previous, replacement = inherited_chain(h, kind)
    reply = h.command('approver', 'bulk.apply', undo_fact(replacement))
    target = previous if changed == 'predecessor' else replacement
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.SchedulingFact, target['id']).revision += 1
    before = state(h)
    refused = h.command('approver', reply['undo']['kind'], reply['undo']['payload'], status=409)
    assert refused['error']['code'] == 'STALE_REVISION'
    assert target['id'] in refused['error']['message']
    assert state(h) == before


def test_paired_fact_inverse_does_not_grant_approval_authority_or_approve_a_proposal(h):
    previous, replacement = chain(h)
    reply = h.command('approver', 'bulk.apply', undo_fact(replacement))
    item = reply['undo']['payload']['items'][0]
    before = state(h)
    refused = h.command('coordinator', item['item_kind'], item['payload'], item['target_id'],
                        item['expected_revision'], status=403)
    assert refused['error']['code'] == 'FORBIDDEN'
    assert state(h) == before
    with transaction(h.app.state.engine) as db:
        previous = record(db.get(m.SchedulingFact, previous['id']))
    waiting = replace(h, previous, 8)
    refused = h.command('approver', 'person.workload.undo', {'reason': 'A proposal is not a previously approved fact',
        'predecessor_revision': previous['revision']}, waiting['id'], waiting['revision'], status=409)
    assert refused['error']['code'] == 'FACT_PREDECESSOR'
    assert state(h)[waiting['id']][0] == 'proposed'


@pytest.mark.parametrize('pending', [False, True])
def test_paired_fact_restore_rechecks_a_sibling_replacing_the_predecessor(h, pending):
    previous, replacement = chain(h)
    undone = h.command('approver', 'bulk.apply', undo_fact(replacement))
    inverse = undone['undo']['payload']['items'][0]
    with transaction(h.app.state.engine) as db:
        previous = record(db.get(m.SchedulingFact, previous['id']))
    proposal = workload(h, 8, replaces_id=previous['id'], expected_revision=previous['revision'])
    proposal['line']['id'] = previous['data']['line']['id']
    if pending:
        with transaction(h.app.state.engine, write=True) as db:
            db.add(m.RoleBinding(identity_id=h.seed['identities']['coordinator'],
                department_id=h.seed['department_id'], role='approver', starts_on=date(2020, 1, 1)))
        before = state(h)
        refused = h.command('coordinator', 'bulk.apply', {'items': [
            {'item_kind': 'person.workload.propose', 'payload': proposal}, inverse],
            'reason': 'A new pending sibling blocks restoring its older replacement'}, status=409)
    else:
        h.command('coordinator', 'person.workload.propose', proposal)
        before = state(h)
        refused = h.command('approver', undone['undo']['kind'], undone['undo']['payload'], status=409)
    assert refused['error']['code'] == 'PROPOSAL_WAITING'
    assert state(h) == before


def test_pending_replacement_in_the_same_batch_blocks_fact_undo(h):
    previous = chain(h, 1)[0]
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.RoleBinding(identity_id=h.seed['identities']['coordinator'],
            department_id=h.seed['department_id'], role='approver', starts_on=date(2020, 1, 1)))
    proposal = workload(h, 4, replaces_id=previous['id'], expected_revision=previous['revision'])
    proposal['line']['id'] = previous['data']['line']['id']
    before = state(h)
    body = {'items': [{'item_kind': 'person.workload.propose', 'payload': proposal},
        {'item_kind': 'person.workload.undo', 'target_id': previous['id'],
         'expected_revision': previous['revision']}], 'reason': 'A pending successor must prevent Undo'}
    refused = h.command('coordinator', 'bulk.apply', body, status=409)
    assert refused['error']['code'] == 'LATER_REPLACEMENT'
    assert state(h) == before


def test_bulk_replacement_approval_keeps_the_independent_reviewer_check(h):
    previous = chain(h, 1)[0]
    with transaction(h.app.state.engine, write=True) as db:
        reviewer = h.seed['identities']['coordinator']
        db.get(m.Identity, reviewer).person_id = previous['person_id']
        db.add(m.RoleBinding(identity_id=reviewer,
            department_id=h.seed['department_id'], role='approver', starts_on=date(2020, 1, 1)))
    replacement = replace(h, previous, 4)
    body = {'item_kind': 'person.workload.activate', 'items': [{'target_id': replacement['id'],
        'expected_revision': replacement['revision']}], 'reason': 'The subject needs permission to approve this replacement'}
    check = preview(h, 'coordinator', body)
    assert check['items'][0]['issues'][0]['code'] == 'SELF_APPROVAL'
    before = state(h)
    refused = h.command('coordinator', 'bulk.apply', body, status=403)
    assert refused['error']['code'] == 'SELF_APPROVAL'
    assert state(h) == before
