"""The generated picker permissions and the live row guard are one definition."""
from apps.api.auth import Actor
from apps.api.commands import command_roles
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services.bulk_selection import _base_query, collection_readers
from packages.contracts import schemas as s
from scripts.generate_contracts import render


def test_registry_readers_match_every_collection_and_role_guard(h):
    registry = s.bulk_collection_registry(command_roles, collection_readers)
    with transaction(h.app.state.engine) as db:
        for role in s.ROLE_NAMES:
            actor = Actor(h.seed['identities']['coordinator'], h.seed['department_id'], h.seed['organisation_id'],
                          'Synthetic reader', frozenset({role}), None, 'synthetic-session')
            for key, entry in registry.items():
                permitted = actor.permits(*entry['readers'])
                try:
                    _base_query(db, actor, key)
                except Problem as error:
                    assert error.code == 'FORBIDDEN' and not permitted, (role, key, error.message)
                else:
                    assert permitted, (role, key)


def test_reader_metadata_uses_current_permission_map_and_matches_http_and_generated_contract(h, monkeypatch):
    monkeypatch.setitem(s.PERMISSIONS, 'audit.read', ('coordinator',))
    monkeypatch.setitem(s.PERMISSIONS, 'system.operations', ('credentialer',))
    monkeypatch.setitem(s.PERMISSIONS, 'access.system_roles', ('staff',))
    expected = s.bulk_collection_registry(command_roles, collection_readers)
    assert expected['history']['readers'] == ['coordinator']
    assert expected['outbox']['readers'] == ['credentialer']
    assert expected['identities']['readers'] == expected['roles']['readers'] == ['admin', 'staff']
    public = h.get('coordinator', '/api/contracts')['bulk_collections']
    assert {key: value['readers'] for key, value in public.items()} == {
        key: value['readers'] for key, value in expected.items()}
    import json
    source, _ = render()
    generated = json.loads(source.split('export const BULK_COLLECTIONS = ', 1)[1].split(' as const;', 1)[0])
    assert generated == public
