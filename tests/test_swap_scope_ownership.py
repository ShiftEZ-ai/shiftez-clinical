"""Swap workspaces scope validation only against their own publication."""
from types import SimpleNamespace
from unittest.mock import Mock

from apps.api.services import published_changes as changes


def test_swap_workspace_does_not_scope_against_another_publication(monkeypatch):
    workspace = SimpleNamespace(id='swap-workspace', base_publication_id='publication-a', department_id='d', status='draft')
    publication_a = SimpleNamespace(id='publication-a', department_id='d')
    publication_b = SimpleNamespace(id='publication-b', department_id='d')
    shift = {'id': 'issued', 'assignment_type_id': 't', 'location_id': 'l', 'service_ids': [],
             'start': '2026-10-04T08:00:00Z', 'end': '2026-10-04T12:00:00Z',
             'type_revision': 1, 'type_snapshot': {}, 'participants': [], 'cancelled': False,
             'locked': False, 'attributes': {}, 'series_id': None, 'workspace_id': workspace.id}
    own = [{**shift, 'id': 'copy'}]
    row = SimpleNamespace(workspace_id='other-workspace', changes=[])
    def scalar(statement):
        return 'swap-id' if statement.compile().params['publication_id_1'] == publication_a.id else None
    db = SimpleNamespace(get=Mock(side_effect=[publication_a, publication_b]),
                         scalar=scalar)
    monkeypatch.setattr(changes, 'change_list', lambda *args: row)
    monkeypatch.setattr(changes.schedule, 'in_force_snapshot', lambda *args: {'shifts': [shift]})
    data = {'shifts': own, 'focus_ids': ['copy'], 'organisation': {'timezone': 'UTC'}}

    assert changes.validation_scope(db, workspace, data)
    workspace.base_publication_id = publication_b.id
    data = {'shifts': own, 'focus_ids': ['copy'], 'organisation': {'timezone': 'UTC'}}
    assert not changes.validation_scope(db, workspace, data)
    assert data['_issued_type_versions'] == {'copy'}
