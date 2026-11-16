"""Publication ancestry is not ownership of a published-change workspace."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.api.services import published_changes as changes


@pytest.mark.parametrize('owner', ['hidden', 'submitted', 'swap', 'ordinary', 'preview'])
def test_only_owned_change_lists_scope_validation_while_all_amendments_keep_issued_types(monkeypatch, owner):
    workspace = SimpleNamespace(id='candidate', base_publication_id='publication', department_id='d')
    publication = SimpleNamespace(id='publication', department_id='d')
    shift = {'id': 'issued', 'assignment_type_id': 't', 'location_id': 'l', 'service_ids': [],
             'start': '2026-10-04T08:00:00Z', 'end': '2026-10-04T12:00:00Z',
             'type_revision': 1, 'type_snapshot': {}, 'participants': [], 'cancelled': False,
             'locked': False, 'attributes': {}, 'series_id': None}
    row = SimpleNamespace(workspace_id='candidate' if owner == 'hidden' else 'hidden',
                          changes=[{'workspace_id': 'candidate'}] if owner == 'submitted' else [])
    own = [{**shift, 'id': 'copy', 'workspace_id': workspace.id}]
    db = SimpleNamespace(get=Mock(return_value=publication), scalar=Mock(return_value=None))
    monkeypatch.setattr(changes, 'change_list', lambda *args: None if owner == 'ordinary' else row)
    monkeypatch.setattr(changes.schedule, 'in_force_snapshot', lambda *args: {'shifts': [shift]})
    monkeypatch.setattr(changes, 'workspace_shifts', lambda *args: own)
    data = {'shifts': own, 'focus_ids': ['copy'], 'organisation': {'timezone': 'UTC'}}
    original = deepcopy(data)
    result = changes.validation_scope(db, workspace, data, **({'own': own} if owner == 'preview' else {}))
    assert result is (owner in {'hidden', 'submitted', 'preview'})
    assert data['_issued_type_versions'] == {'copy'}
    if not result:
        assert data == {**original, '_issued_type_versions': {'copy'}}
    else:
        assert data['person_rule_subjects'] == data['quota_subjects'] == set()
        assert data['_published_session_checks'] == set()


def test_foreign_department_publication_cannot_scope_or_mutate_validation(monkeypatch):
    workspace = SimpleNamespace(id='w', base_publication_id='pub', department_id='d')
    db = SimpleNamespace(get=Mock(return_value=SimpleNamespace(department_id='other')))
    data = {'focus_ids': ['unchanged']}
    assert not changes.validation_scope(db, workspace, data)
    assert data == {'focus_ids': ['unchanged']}


def test_owned_changes_keep_people_simultaneous_peers_and_quota_days(monkeypatch):
    workspace = SimpleNamespace(id='candidate', base_publication_id='pub', department_id='d')
    publication = SimpleNamespace(id='pub', department_id='d')
    person = lambda pid: {'person_id': pid, 'start': '2026-10-04T08:00:00Z',
        'end': '2026-10-04T12:00:00Z', 'participation': 'staff', 'overtime': False}
    base = {'id': 'old', 'assignment_type_id': 't', 'location_id': 'l', 'service_ids': [],
        'start': '2026-10-04T08:00:00Z', 'end': '2026-10-04T12:00:00Z',
        'type_revision': 1, 'type_snapshot': {}, 'participants': [person('first')],
        'cancelled': False, 'locked': False, 'attributes': {}, 'series_id': None}
    peer = {**base, 'id': 'old-peer', 'location_id': 'peer', 'participants': []}
    later = {**base, 'id': 'old-later', 'start': '2026-10-08T08:00:00Z',
             'end': '2026-10-08T12:00:00Z', 'participants': []}
    own = [{**base, 'id': 'changed', 'participants': [person('second')]},
           {**peer, 'id': 'peer'}, {**later, 'id': 'later'}]
    own = [{**row, 'workspace_id': workspace.id} for row in own]
    neighbour = {**later, 'id': 'other-rota', 'participants': [person('first')]}
    db = SimpleNamespace(get=Mock(return_value=publication), scalar=Mock(return_value=None))
    monkeypatch.setattr(changes, 'change_list', lambda *args: SimpleNamespace(workspace_id=workspace.id, changes=[]))
    monkeypatch.setattr(changes.schedule, 'in_force_snapshot', lambda *args: {'shifts': [base, peer, later]})
    monkeypatch.setattr(changes, 'workspace_shifts', lambda *args: own)
    data = {'shifts': [*own, neighbour], 'focus_ids': ['changed', 'peer', 'later'], 'organisation': {'timezone': 'UTC'}}
    assert changes.validation_scope(db, workspace, data)
    assert data['_published_session_checks'] == {'changed', 'peer'}
    assert data['focus_ids'] == ['changed', 'other-rota', 'peer']
    assert data['person_rule_subjects'] == data['quota_subjects'] == {'first', 'second'}
    assert data['quota_dates'] == ('2026-10-04', '2026-10-04')
    assert data['_issued_type_versions'] == {'changed', 'peer', 'later'}
