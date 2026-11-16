"""Session selection and related ordering follow assignment-only planner changes."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk_linked_people import post


@pytest.mark.parametrize('path', ['condition', 'filter', 'sort_group'])
def test_cached_session_person_projection_follows_assignment_only_draft_swap(h, path):
    first = h.shift(h.workspace())
    workspace = h.shift(first['workspace'], start='2026-10-05T08:00:00+03:00',
        end='2026-10-05T12:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    original = sorted(h.detail(workspace)['shifts'], key=lambda row: row['start'])
    selection = {'collection': 'sessions', 'where': {'as_of': '2026-10-04',
        'filters': {'workspace_id': [workspace['id']]}}}
    if path == 'condition':
        selection['where']['conditions'] = {'conditions': [
            {'field': 'person_ids', 'values': [h.seed['people'][0]]}]}
    elif path == 'filter':
        selection['where']['filters']['person_ids'] = [h.seed['people'][0]]
    payload = {'selection': selection, 'columns': ['person_ids', 'person.display_name'],
        'sort': [{'field': 'person.display_name'}], 'group': 'person.display_name', 'limit': 1}
    before = post(h, 'rows', payload, role='coordinator')
    assert before['count'] == (2 if path == 'sort_group' else 1)
    assert post(h, 'rows', payload, role='coordinator') == before
    h.command('coordinator', 'draft.swap', {'workspace_id': workspace['id'],
        'first_assignment_id': original[0]['participants'][0]['id'],
        'second_assignment_id': original[1]['participants'][0]['id'],
        'reason': 'Synthetic assignment-only collection cache change'}, revision=workspace['revision'])
    with transaction(h.app.state.engine) as db:
        assert dict(db.execute(select(m.Shift.id, m.Shift.revision).where(
            m.Shift.workspace_id == workspace['id'])).all()) == {row['id']: row['revision'] for row in original}
    live = post(h, 'resolve', selection, role='coordinator')
    first_page = post(h, 'rows', payload, role='coordinator')
    later_page = post(h, 'rows', {**payload, 'offset': 1}, role='coordinator')
    assert first_page['count'] == later_page['count'] == live['count'] == before['count']
    if path == 'sort_group':
        rows = first_page['rows'] + later_page['rows']
        assert [row['person.display_name'] for row in rows] == sorted(row['person.display_name'] for row in rows)
        assert first_page['rows'][0]['id'] != before['rows'][0]['id']
        assert first_page['groups'] == later_page['groups']
    else:
        assert [row['id'] for row in live['sample']] == [original[1]['id']]
        assert [row['id'] for row in first_page['rows']] == [original[1]['id']]
        assert first_page['rows'][0]['person_ids'] == [h.seed['people'][0]] and not later_page['rows']
