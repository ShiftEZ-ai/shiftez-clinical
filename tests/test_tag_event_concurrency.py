"""Shared event labels participate in event saves without losing pending delivery."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from tests.outbox_support import process_one_after_undo as process_one
from .test_events import definition, event, input_of, occurrences


def change_members(h, tag, row, *, remove=False, atomic=False):
    payload = {'remove' if remove else 'add': [{'kind': 'events', 'id': row['id']}]}
    if atomic:
        payload = {'catalogue': 'tags', 'value': {'name': tag['name'], 'labels': tag['labels']},
                   'members': payload}
    return h.command('coordinator', 'catalogue.update' if atomic else 'tag.members',
        {**payload, 'reason': 'Update shared event labels'}, tag['id'], tag['revision'])['record']


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('writer', ['tag_members', 'catalogue_members', 'event_save'])
@pytest.mark.parametrize('remove', [False, True], ids=['add', 'remove'])
def test_shared_event_label_changes_reject_stale_event_saves(request, backend, writer, remove):
    h = request.getfixturevalue(backend)
    tag = h.configure('tags', {'name': 'Shared event label', 'labels': ['events']})['record']
    row = event(h, tag_ids=[tag['id']] if remove else [])
    stale = definition(h, row)
    if writer == 'event_save':
        h.configure('events', input_of(stale, tag_ids=[] if remove else [tag['id']]),
            row['id'], stale['revision'], role='admin')
    else:
        change_members(h, tag, row, remove=remove, atomic=writer == 'catalogue_members')
    current = definition(h, row)
    assert current['revision'] == stale['revision'] + 1
    assert current['tag_ids'] == ([] if remove else [tag['id']])
    reply = h.configure('events', input_of(stale, heading='Stale event save'), row['id'],
        stale['revision'], role='coordinator', status=409)
    assert reply['error']['code'] == 'STALE_REVISION'
    assert reply['error']['details']['current_revision'] == current['revision']
    assert definition(h, row) == current
    saved = h.configure('events', input_of(current, heading='Reconciled event save'), row['id'],
        current['revision'], role='coordinator')['record']
    assert saved['revision'] == current['revision'] + 1
    reloaded = definition(h, row)
    assert reloaded['heading'] == 'Reconciled event save' and reloaded['tag_ids'] == current['tag_ids']


@pytest.mark.parametrize('pending_state', ['queued', 'retry'])
def test_shared_event_label_change_preserves_pending_occurrence_delivery(h, pending_state):
    tag = h.configure('tags', {'name': 'Pending event label', 'labels': ['events']})['record']
    row = event(h)
    occurrence = next(item for item in occurrences(h) if item['event_id'] == row['id'])
    with transaction(h.app.state.engine, write=True) as db:
        pending = db.scalar(select(m.Outbox).where(m.Outbox.kind == 'event',
            m.Outbox.payload['event_id'].as_string() == row['id']))
        pending.state = pending_state
    change_members(h, tag, row)
    process_one(h.app.state.engine)
    notifications = [item for item in h.get('staff', '/api/notifications')['notifications']
                     if item['source_id'] == occurrence['id']]
    assert len(notifications) == 1 and notifications[0]['title'] == row['heading']
    delivered = next(item for item in occurrences(h, 'staff') if item['id'] == occurrence['id'])
    assert delivered['receipt']['delivered_at'] is not None


def test_shared_event_label_noop_keeps_revision_and_delivered_changes_do_not_resend(h):
    tag = h.configure('tags', {'name': 'Delivered event label', 'labels': ['events']})['record']
    row = event(h)
    process_one(h.app.state.engine)
    before = h.get('staff', '/api/notifications')
    tag = change_members(h, tag, row)
    revised = definition(h, row)
    change_members(h, tag, row)
    assert definition(h, row)['revision'] == revised['revision']
    process_one(h.app.state.engine)
    assert h.get('staff', '/api/notifications') == before
