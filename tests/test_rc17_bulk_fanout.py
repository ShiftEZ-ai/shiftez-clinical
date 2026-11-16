"""A bulk row's transitive writes share the configured record ceiling."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from .test_bulk import REASON, preview
from .test_bulk_nested_limits import limits
from .test_events import event_type, value


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def fanout_h(request):
    return request.getfixturevalue(request.param)


def state(h):
    with transaction(h.app.state.engine) as db:
        return {cls.__tablename__: [record(row) for row in db.scalars(select(cls).order_by(cls.id))]
                for cls in (m.Tag, m.TagMember, m.Person, m.Event, m.EventOccurrence, m.DepartmentContent,
                            m.AnnouncementReceipt, m.Outbox, m.Notification, m.DefinitionHistory, m.AuditEvent,
                            m.CommandReceipt)}


@pytest.mark.parametrize('action', ['rename', 'members'])
def test_tag_fanout_and_undo_count_people_as_well_as_members(fanout_h, action):
    h = fanout_h
    tag = h.configure('tags', {'name': 'Synthetic fanout tag', 'labels': ['people']})['record']
    people = [h.configure('people', {'display_name': f'Synthetic fanout colleague {n}',
        'tags': [tag['name']] if action == 'rename' else []})['record'] for n in range(2)]
    if action == 'rename':
        body = {'item_kind': 'catalogue.update', 'common': {'catalogue': 'tags'}, 'items': [{
            'target_id': tag['id'], 'expected_revision': tag['revision'],
            'changes': [{'field': 'name', 'value': 'Renamed fanout tag'}]}], 'reason': REASON}
        expected = 3
    else:
        body = {'item_kind': 'tag.members', 'items': [{'target_id': tag['id'],
            'expected_revision': tag['revision'], 'payload': {'add': [
                {'kind': 'people', 'id': person['id']} for person in people]}}], 'reason': REASON}
        expected = 5
    h.client('coordinator')
    for undo in (False, True):
        for maximum in (1, expected - 1):
            limits(h, maximum)
            before = state(h)
            checked = h.client('coordinator').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': body})
            preflight = action == 'members' and maximum == 1
            assert checked.status_code == (422 if preflight else 200), checked.text
            issue = checked.json()['error'] if preflight else checked.json()['items'][0]['issues'][0]
            assert issue['code'] == 'BULK_ITEM_LIMIT'
            assert h.command('coordinator', 'bulk.apply', body,
                status=422 if undo or preflight else 409)['error']['code'] == 'BULK_ITEM_LIMIT'
            assert state(h) == before
        limits(h, expected)
        applied = h.command('coordinator', 'bulk.apply', body)
        assert applied['items'][0]['results'][0]['affected_count'] == expected
        with transaction(h.app.state.engine) as db:
            names = [db.get(m.Person, person['id']).tags for person in people]
        assert names == ([[tag['name']]] * 2 if undo and action == 'rename' else
                         [[]] * 2 if undo else
                         [['Renamed fanout tag']] * 2 if action == 'rename' else [[tag['name']]] * 2)
        body = applied['undo']['payload']


def test_event_occurrences_share_the_bulk_limit_with_the_definition(fanout_h):
    h = fanout_h
    typ = event_type(h)
    body = {'item_kind': 'catalogue.create', 'items': [{'payload': {'catalogue': 'events',
        'value': value(typ)}}], 'reason': REASON}
    h.client('coordinator')
    limits(h, 1)
    before = state(h)
    assert preview(h, 'coordinator', body)['items'][0]['issues'][0]['code'] == 'BULK_ITEM_LIMIT'
    assert h.command('coordinator', 'bulk.apply', body, status=409)['error']['code'] == 'BULK_ITEM_LIMIT'
    assert state(h) == before
    limits(h, 2)
    applied = h.command('coordinator', 'bulk.apply', body)
    assert applied['items'][0]['results'][0]['affected_count'] == 2
    limits(h, 1)
    before = state(h)
    assert preview(h, 'coordinator', applied['undo']['payload'])['items'][0]['issues'][0]['code'] == 'BULK_ITEM_LIMIT'
    assert h.command('coordinator', **applied['undo'], status=422)['error']['code'] == 'BULK_ITEM_LIMIT'
    assert state(h) == before
    limits(h, 2)
    undone = h.command('coordinator', **applied['undo'])
    assert undone['items'][0]['results'][0]['affected_count'] == 2


def test_announcement_recipient_receipts_share_the_bulk_limit(fanout_h):
    h = fanout_h
    body = {'item_kind': 'catalogue.create', 'items': [{'payload': {'catalogue': 'department_content',
        'value': {'name': 'Synthetic counted receipt notice', 'description': 'Synthetic receipt fanout',
                  'starts_on': '2020-01-01', 'audience': {'roles': ['staff', 'coordinator']},
                  'notify': False}}}], 'reason': REASON}
    h.client('coordinator')
    limits(h, 1)
    before = state(h)
    assert preview(h, 'coordinator', body)['items'][0]['issues'][0]['code'] == 'BULK_ITEM_LIMIT'
    assert h.command('coordinator', 'bulk.apply', body, status=409)['error']['code'] == 'BULK_ITEM_LIMIT'
    assert state(h) == before
    limits(h, 20)
    applied = h.command('coordinator', 'bulk.apply', body)
    row = applied['items'][0]['record']
    with transaction(h.app.state.engine) as db:
        receipts = list(db.scalars(select(m.AnnouncementReceipt.id).where(m.AnnouncementReceipt.content_id == row['id'])))
    assert len(receipts) >= 2
    assert applied['items'][0]['results'][0]['affected_count'] == len(receipts) + 1
    h.command('coordinator', **applied['undo'])
    with transaction(h.app.state.engine) as db:
        assert not db.get(m.DepartmentContent, row['id']).active
        assert set(db.scalars(select(m.AnnouncementReceipt.id).where(m.AnnouncementReceipt.content_id == row['id']))) == set(receipts)
