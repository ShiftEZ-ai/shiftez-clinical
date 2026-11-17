import pytest
from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import policies
from packages.contracts.schemas import Command
from .test_contextual_feasibility import publication_digest


def test_batch_activation_checks_combined_impact_and_preserves_publications(h, monkeypatch):
    def propose(maximum):
        p = h.command('coordinator', 'policy.create', {'name': 'Batch limit', 'owner': 'QA',
            'source': 'Synthetic batch test', 'starts_on': '2020-01-01',
            'definition': {'rule': {'kind': 'max_duties', 'maximum': maximum, 'window': 'day'}}})['record']
        return Command(kind='policy.activate', target_id=p['id'], expected_revision=p['revision'],
                       payload={'reason': 'Independent test approval'})
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00')['workspace']
    published = h.publish(w)
    with transaction(h.app.state.engine) as db:
        issued = publication_digest(db, published['publication_id'])
    # Both rotas are checked; issued publication evidence stays immutable.
    draft = h.shift(h.workspace(start='2026-10-11', end='2026-10-18'),
                    start='2026-10-11T08:00:00+03:00', end='2026-10-11T12:00:00+03:00')['workspace']
    client = h.client('approver')
    calls = []
    original = policies.evaluate
    def evaluate(data):
        calls.append(data['workspace']['id'])
        return original(data)
    monkeypatch.setattr(policies, 'evaluate', evaluate)
    commands = [propose(2), propose(3)]
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        result = policies.activate_policies(db, actor, commands)
        assert all(p['status'] == 'active' for p in result['records'])
    assert len(calls) == 2  # One combined evaluation per affected rota.
    assert calls == [published['workspace']['id'], draft['id']]
    with transaction(h.app.state.engine) as db:
        assert publication_digest(db, published['publication_id']) == issued
    # Legacy activation reports stricter impact without rewriting issued evidence.
    commands = [propose(4), propose(1)]
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        assert all(p['status'] == 'active' for p in policies.activate_policies(db, actor, commands)['records'])
        assert publication_digest(db, published['publication_id']) == issued
