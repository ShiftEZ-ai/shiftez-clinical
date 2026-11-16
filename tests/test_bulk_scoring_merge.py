"""Scoring-default admission retains the batch's pending lifecycle and provenance."""
from copy import deepcopy
from datetime import date

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.catalogue_version import department_version
from apps.api.services.common import history
from packages.contracts.schemas import PolicyDefinition
from .test_bulk import REASON, preview
from .test_bulk_selection import actor


DATABASES = ['h', pytest.param('pg', marks=pytest.mark.postgres)]
RULE = {'kind': 'balance_goal', 'metric': 'workload', 'aim': 'workload_spread'}


@pytest.mark.parametrize('database', DATABASES)
@pytest.mark.parametrize('transition', ['activate', 'retire', 'create', 'delete'])
def test_department_catalogue_version_reads_pending_lifecycle(request, database, transition):
    h = request.getfixturevalue(database)
    proposal = h.command('coordinator', 'policy.create', {
        'name': 'Legacy admission source', 'owner': 'Department', 'source': REASON,
        'starts_on': '2020-01-01', 'definition': {'rule': {'kind': 'min_rest', 'hours': 10}}})['record']
    if transition in {'retire', 'delete'}:
        proposal = h.command('approver', 'policy.activate', {'reason': REASON},
                             proposal['id'], proposal['revision'])['record']
    with transaction(h.app.state.engine, write=True) as db, db.no_autoflush:
        current = db.get(m.Policy, proposal['id'])
        if transition == 'create':
            db.add(m.Policy(department_id=current.department_id, name='Pending legacy source',
                kind=current.kind, definition=deepcopy(current.definition), starts_on=date(2020, 1, 1),
                active=True, status='active', owner='Department', source=REASON,
                created_by=current.created_by, approved_by=h.seed['identities']['approver']))
        elif transition == 'delete':
            db.delete(current)
        else:
            current.status = 'active' if transition == 'activate' else 'retired'
        assert department_version(db, h.seed['department_id']) == (
            1 if transition in {'activate', 'create'} else 2)
        db.rollback()


@pytest.mark.parametrize('database', DATABASES)
def test_default_lineage_uses_queued_history_before_flush(request, database):
    h = request.getfixturevalue(database)
    with transaction(h.app.state.engine, write=True) as db, db.no_autoflush:
        current_actor = actor(db, h)
        seed = db.scalar(select(m.Policy).where(m.Policy.department_id == h.seed['department_id']))
        seed.kind = 'balance_goal'
        seed.definition = PolicyDefinition.model_validate({'rule': RULE, 'severity': 'advisory'}).model_dump(mode='json')
        seed.status, seed.revision = 'retired', seed.revision + 1
        db.info['bulk'] = {'history': []}
        history(db, current_actor, 'policy', seed, REASON)
        successor = m.Policy(department_id=seed.department_id, name='Pending default adjustment', kind=seed.kind,
            definition={**deepcopy(seed.definition), 'replaces_id': seed.id}, starts_on=seed.starts_on,
            active=True, status='active', owner='Department', source=REASON,
            created_by=current_actor.identity_id, approved_by=h.seed['identities']['approver'])
        db.add(successor)
        history(db, current_actor, 'policy', successor, REASON)
        assert department_version(db, seed.department_id, [successor]) == 2
        assert department_version(db, seed.department_id) == 2
        db.info.pop('bulk')
        db.rollback()


@pytest.mark.parametrize('database', DATABASES)
def test_converted_scoring_default_adjustment_activates_and_bulk_undo_restores(request, database):
    h = request.getfixturevalue(database)
    with transaction(h.app.state.engine, write=True) as db:
        seed = db.scalar(select(m.Policy).where(m.Policy.department_id == h.seed['department_id']))
        seed.kind = 'balance_goal'
        seed.definition = PolicyDefinition.model_validate({'rule': RULE, 'severity': 'advisory'}).model_dump(mode='json')
        seed.revision += 1
        current_actor = actor(db, h)
        history(db, current_actor, 'policy', seed, REASON)
        db.add(m.CatalogueConversion(department_id=seed.department_id, status='active',
            proposed_by=current_actor.identity_id, approved_by=h.seed['identities']['approver'], approved_at=m.now(),
            reason=REASON, input_revision=1, plan_hash='0' * 64, manifest={}, plan={}, proof={}))
        seed_id, seed_revision = seed.id, seed.revision
    proposed = h.command('coordinator', 'policy.create', {
        'name': 'Reviewed default adjustment', 'owner': 'Department', 'source': REASON,
        'starts_on': '2000-01-01', 'definition': {'rule': RULE, 'severity': 'advisory',
                                               'replaces_id': seed_id}})['record']
    value = {'items': [{'item_kind': 'policy.activate', 'target_id': proposed['id'],
                       'expected_revision': proposed['revision'], 'payload': {}}], 'reason': REASON}
    assert preview(h, 'approver', value)['counts']['problem'] == 0
    applied = h.command('approver', 'bulk.apply', value)
    assert applied['items'][0]['record']['status'] == 'active'
    h.command('approver', **applied['undo'])
    with transaction(h.app.state.engine) as db:
        restored = db.get(m.Policy, seed_id)
        assert restored.status == 'active' and restored.active is True
        assert restored.revision == seed_revision + 2
        assert db.get(m.Policy, proposed['id']).status == 'retired'
