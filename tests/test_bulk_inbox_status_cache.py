"""Passive Home reads change inbox status without invalidating acknowledgement revisions."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from tests.outbox_support import process_one_after_undo as process_one
from .test_announcement_receipts import notice, notes, seen
from .test_bulk_linked_people import post


@pytest.mark.parametrize('path', ['condition', 'filter'])
def test_cached_inbox_status_follows_home_seen_without_changing_acknowledgement_revision(h, path):
    content = notice(h, {'roles': ['staff']}, importance='must_acknowledge', notify=True)
    process_one(h.app.state.engine)
    note = notes(h, content['id'])[0]
    selection = {'collection': 'inbox', 'where': {'filters': {'id': [note['id']]}}}
    if path == 'condition':
        selection['where']['conditions'] = {'conditions': [{'field': 'status', 'values': ['unread']}]}
    else:
        selection['where']['filters']['status'] = ['unread']
    payload = {'selection': selection, 'columns': ['status'], 'sort': [{'field': 'status'}], 'group': 'status'}
    assert post(h, 'rows', payload, role='staff')['count'] == 1
    assert post(h, 'rows', payload, role='staff')['count'] == 1
    seen(h, [content['id']])
    with transaction(h.app.state.engine) as db:
        current = db.get(m.Notification, note['id'])
        assert current.read_at is not None and current.acknowledged_at is None
        assert current.revision == note['revision']
    assert post(h, 'resolve', selection, role='staff')['count'] == 0
    empty = post(h, 'rows', payload, role='staff')
    assert empty['count'] == 0 and empty['rows'] == empty['groups'] == []
    if path == 'condition':
        selection['where']['conditions']['conditions'][0]['values'] = ['read']
    else:
        selection['where']['filters']['status'] = ['read']
    assert post(h, 'resolve', selection, role='staff')['count'] == 1
    row = post(h, 'rows', payload, role='staff')['rows'][0]
    assert row['id'] == note['id'] and row['status'] == 'read' and row['revision'] == note['revision']
