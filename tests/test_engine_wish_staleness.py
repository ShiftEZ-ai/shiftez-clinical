"""Wish changes update proposal application without discarding completed native work."""
import copy
from dataclasses import replace

import pytest

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.services.jobs import create_job
from packages.contracts.schemas import Command
from packages.domain.cp_sat import generate


@pytest.mark.postgres
@pytest.mark.parametrize('mutation', ['create', 'update', 'withdraw', 'standing_create', 'standing_update',
                                      'standing_withdraw', 'standing_override'])
def test_wish_mutation_reconciles_pending_generation(pg, mutation):
    h = pg
    workspace = h.shift(h.workspace(), people=[])['workspace']
    payload = {'person_id': h.seed['people'][0], 'first_day': '2026-10-04', 'last_day': '2026-10-04',
               'kind': 'prefer_on', 'points': 1}
    if mutation.startswith('standing_'):
        payload['repeat'] = {'weekdays': [6], 'starts_on': '2026-10-04'}
    wish = None
    if mutation not in ('create', 'standing_create'):
        wish = h.command('coordinator', 'wish.create', payload)['wish']
    before = h.snapshot(workspace)
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=workspace['revision'],
            payload={'workspace_id': workspace['id'], 'kind': 'generation'}),
            replace(h.settings, generation_engine='cp_sat'))['job']
    h.process_jobs(scheduler=lambda snapshot, **kwargs: generate(
        snapshot, 3, workers=1, deterministic=True, **kwargs))
    completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert completed['status'] == 'succeeded'
    assert completed['result']['status'] == 'candidate_feasible'
    assert completed['result']['additions']
    with transaction(h.app.state.engine) as db:
        frozen_input = copy.deepcopy(db.get(m.Job, job['id']).input_snapshot)
    assert frozen_input['wishes'] == before['wishes']

    if mutation in ('create', 'standing_create'):
        h.command('coordinator', 'wish.create', {**payload, 'kind': 'prefer_off'})
    elif mutation in ('update', 'standing_update'):
        h.command('coordinator', 'wish.update', {**payload, 'points': 2}, wish['id'], wish['revision'])
    elif mutation == 'standing_override':
        h.command('coordinator', 'wish.override', {'workspace_id': workspace['id'], 'skip': True}, wish['id'], wish['revision'])
    else:
        h.command('coordinator', 'wish.withdraw', {'reason': 'Plans changed'}, wish['id'], wish['revision'])
    current = h.snapshot(workspace)
    assert current['wishes'] != before['wishes']

    applied = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
        'job_id': job['id'], 'reason': 'Review proposal against current wishes'}, revision=workspace['revision'])
    assert applied['reconciliation']['status'] == 'applied'
    assert applied['reconciliation']['conflicting_placement_count'] == 0
    assert current['fingerprint'] != before['fingerprint']
    assert current['organisation']['input_revision'] == before['organisation']['input_revision'] + 1
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.status == 'applied'
        assert row.input_snapshot == frozen_input
        assert row.result['additions'] == completed['result']['additions']
