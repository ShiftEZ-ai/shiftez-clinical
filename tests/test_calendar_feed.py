"""A person subscribes to their duties with a private link; a new link or revoking ends the old one."""
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from .clock_support import freeze_clock
from apps.api import models as m
from apps.api.commands import idempotency_hash_input
from apps.api.db import transaction
from apps.api.auth import digest
from apps.api.snapshots import snapshot_hash
from packages.contracts.schemas import Command
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from .conftest import TEST_PASSWORD
from packages.domain.reports import METRIC_VERSION, calendar_feed, publication_duty_ids
from packages.domain.time import aware


def events(response):
    text = response if isinstance(response, str) else response.text
    unfolded = text.replace('\r\n ', '')
    return [dict(line.split(':', 1) for line in block.split('END:VEVENT', 1)[0].split('\r\n') if ':' in line)
            for block in unfolded.split('BEGIN:VEVENT\r\n')[1:]]


def test_private_calendar_secret_is_returned_once_without_persisting_in_receipts(h):
    key = str(uuid.uuid4())
    result = h.command('staff', 'calendar_feed.create', {}, key=key)
    secret = result['path'].split('/')[-1].removesuffix('.ics')
    with transaction(h.app.state.engine) as db:
        feed = db.scalar(select(m.CalendarFeed))
        assert feed.token_hash == digest(secret)
        stored = json.dumps([r.result for r in db.scalars(select(m.CommandReceipt))])
        stored += json.dumps([e.detail for e in db.scalars(select(m.AuditEvent))])
        secret_is_persisted = secret in stored
        assert secret_is_persisted is False
    repeated = h.command('staff', 'calendar_feed.create', {}, key=key)
    recovered = h.get('staff', '/api/commands/' + key)['result']
    assert 'path' not in repeated and 'path' not in recovered
    assert repeated['created'] is True and recovered['created'] is True
    assert repeated['receipt_id'] == recovered['receipt_id'] == result['receipt_id']
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        assert client.get(result['path']).status_code == 200
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.CalendarFeed)))) == 1


def test_historical_calendar_receipts_never_replay_the_secret_or_rewrite_evidence(h):
    key = str(uuid.uuid4())
    command = Command(kind='calendar_feed.create', payload={})
    original = {'path': '/calendar/legacy-private-secret.ics', 'receipt_id': str(uuid.uuid4())}
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.CommandReceipt(id=original['receipt_id'], department_id=h.seed['department_id'],
            actor_id=h.seed['identities']['staff'], command_kind=command.kind, required_roles=['staff'],
            key=key, payload_hash=snapshot_hash(idempotency_hash_input(command)), result=original))
    repeated = h.command('staff', command.kind, {}, key=key)
    recovered = h.get('staff', '/api/commands/' + key)['result']
    assert repeated == recovered == {'receipt_id': original['receipt_id']}
    with transaction(h.app.state.engine) as db:
        assert db.get(m.CommandReceipt, original['receipt_id']).result == original
        assert list(db.scalars(select(m.CalendarFeed))) == []


@pytest.mark.parametrize('action', ['status', 'create', 'revoke'])
def test_calendar_links_are_isolated_between_departments(h, action):
    path = h.command('staff', 'calendar_feed.create', {})['path']
    with transaction(h.app.state.engine, write=True) as db:
        source = db.get(m.Department, h.seed['department_id'])
        other = m.Department(organisation_id=source.organisation_id, name='Other department')
        db.add(other)
        db.flush()
        other_id = other.id
        db.add(m.RoleBinding(identity_id=h.seed['identities']['staff'], department_id=other_id,
                             role='staff', starts_on=date(2020, 1, 1)))
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        client.headers['origin'] = h.settings.public_origin
        signed_in = client.post('/auth/login', json={'username': 'staff', 'password': TEST_PASSWORD,
                                                    'department_id': other_id})
        assert signed_in.status_code == 200
        client.headers['x-csrf-token'] = client.cookies['rota_csrf']
        if action == 'status':
            assert client.get('/api/calendar-feed').json()['active'] is False
        else:
            result = client.post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())},
                                 json={'kind': 'calendar_feed.' + action, 'payload': {}})
            assert result.status_code == 200
            assert h.get('staff', '/api/calendar-feed')['active'] is True
        assert client.get(path).status_code == 200


@pytest.fixture
def subscription(h):
    me = h.get('staff', '/api/me')['person_id']
    w = h.shift(h.workspace(), people=[me])['workspace']
    publication = h.publish(w)
    path = h.command('staff', 'calendar_feed.create', {})['path']
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        yield client, path, publication, w


def test_private_calendar_link_lists_only_my_published_duties_and_can_be_revoked(h):
    me = h.get('staff', '/api/me')['person_id']
    other = next(p for p in h.seed['people'] if p != me)
    w = h.shift(h.workspace(), people=[me])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[other])['workspace']
    h.publish(w)
    assert h.get('staff', '/api/calendar-feed')['active'] is False
    path = h.command('staff', 'calendar_feed.create', {})['path']
    client = TestClient(h.app, base_url=h.settings.public_origin)  # no sign-in: a calendar app
    feed = client.get(path)
    assert feed.status_code == 200 and feed.headers['content-type'].startswith('text/calendar')
    department_name = h.get('staff', '/api/me')['department']['name']
    feed_text = feed.text.replace('\r\n ', '').replace('\\;', ';')
    assert f'X-WR-CALNAME:{department_name} rota' in feed_text
    assert 'Your calendar app updates this on its own schedule; it may take up to a day.' in feed_text
    assert 'Updates when the rota changes' not in feed_text
    assert feed.text.count('BEGIN:VEVENT') == 1 and other not in feed.text
    assert h.get('staff', '/api/calendar-feed')['active'] is True
    # A new link replaces the old one; revoking ends it.
    newer = h.command('staff', 'calendar_feed.create', {})['path']
    assert client.get(path, headers={'If-None-Match': feed.headers['etag']}).status_code == 404
    current = client.get(newer)
    assert current.status_code == 200
    h.command('staff', 'calendar_feed.revoke', {})
    revoked = client.get(newer, headers={'If-None-Match': current.headers['etag']})
    assert revoked.status_code == 404 and revoked.headers['cache-control'] == 'no-store'
    assert client.get('/calendar/not-a-real-link.ics', headers={'If-None-Match': '*'}).status_code == 404
    client.close()


def test_conditional_refresh_has_a_strong_content_etag_and_updates_last_used(h, subscription, monkeypatch):
    client, path, publication, _ = subscription
    first = client.get(path)
    tag = first.headers['etag']
    assert first.status_code == 200
    assert tag == '"' + hashlib.sha256(first.content).hexdigest() + '"'
    assert first.headers['cache-control'] == 'private, no-cache'
    assert 'content-encoding' not in first.headers
    before = aware(h.get('staff', '/api/calendar-feed')['last_used_at'])
    later = before + timedelta(hours=1)
    monkeypatch.setattr('apps.api.services.calendar_feeds.utc_now', lambda: later)
    repeat = client.get(path, headers={'If-None-Match': tag})
    assert repeat.status_code == 304 and repeat.content == b''
    assert repeat.headers['etag'] == tag
    assert repeat.headers['cache-control'] == 'private, no-cache'
    assert aware(h.get('staff', '/api/calendar-feed')['last_used_at']) == later
    assert client.get(path).content == first.content
    with transaction(h.app.state.engine) as db:
        issued = aware(db.get(m.Publication, publication['publication_id']).created_at).strftime('%Y%m%dT%H%M%SZ')
    event, = events(first)
    assert event['DTSTAMP'] == event['LAST-MODIFIED'] == issued
    assert event['SEQUENCE'] == str(publication['sequence'])
    assert h.client('staff').get('/api/calendar-feed').headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('condition', ['weak', 'list', 'wildcard', 'miss'])
def test_if_none_match_comparison(subscription, condition):
    client, path, _, _ = subscription
    first = client.get(path)
    tag = first.headers['etag']
    value = {'weak': 'W/' + tag, 'list': '"old", W/' + tag, 'wildcard': '*', 'miss': '"different"'}[condition]
    response = client.get(path, headers={'If-None-Match': value})
    assert response.status_code == (200 if condition == 'miss' else 304)
    assert response.headers['etag'] == tag


def test_amendment_keeps_unchanged_duty_uid_and_changes_etag(h, subscription):
    client, path, original, _ = subscription
    first = client.get(path)
    old, = events(first)
    previous = h.get('coordinator', '/api/publications/' + original['publication_id'])['snapshot']['shifts'][0]
    amendment = h.workspace('Amendment', base=original['publication_id'])
    copied, = h.detail(amendment)['shifts']
    assert copied['id'] != previous['id']  # row IDs are intentionally fresh across copies
    published = h.publish(amendment)
    changed = client.get(path, headers={'If-None-Match': first.headers['etag']})
    current, = events(changed)
    assert changed.status_code == 200 and changed.headers['etag'] != first.headers['etag']
    assert current['UID'] == old['UID']
    assert current['SEQUENCE'] == str(published['sequence'])
    for field in ('DTSTART', 'DTEND', 'SUMMARY', 'LOCATION', 'DESCRIPTION'):
        assert current[field] == old[field]
    assert client.get(path, headers={'If-None-Match': changed.headers['etag']}).status_code == 304


def move_duty(h, amendment, *, offset=None, location=None):
    copied, = h.detail(amendment)['shifts']
    payload = {key: copied[key] for key in ('assignment_type_id', 'location_id', 'service_ids',
                                            'start', 'end', 'attributes')}
    if offset is not None:
        for key in ('start', 'end'):
            payload[key] = (aware(copied[key]) + offset).isoformat()
    if location is not None:
        payload['location_id'] = location
    payload.update(workspace_id=amendment['id'], shift_id=copied['id'], participants=[{
        'person_id': copied['participants'][0]['person_id'], 'start': payload['start'], 'end': payload['end'],
    }])
    return h.command('coordinator', 'shift.update', payload, revision=amendment['revision'])['workspace']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('move', ['time', 'location'])
def test_amendment_keeps_duty_uid_when_a_published_duty_moves(h, subscription, move):
    client, path, original, _ = subscription
    first = client.get(path)
    before, = events(first)
    amendment = h.workspace('Moved duty', base=original['publication_id'])
    if move == 'time':
        edited = move_duty(h, amendment, offset=timedelta(hours=1))
        changed_field = 'DTSTART'
    else:
        edited = move_duty(h, amendment, location=h.seed['locations'][1])
        changed_field = 'LOCATION'

    published = h.publish(edited)
    refreshed = client.get(path, headers={'If-None-Match': first.headers['etag']})
    assert refreshed.status_code == 200 and refreshed.headers['etag'] != first.headers['etag']
    moved, = events(refreshed)
    assert moved['UID'] == before['UID']
    assert moved[changed_field] != before[changed_field]
    assert moved['SEQUENCE'] == str(published['sequence'])

    h.publish(h.workspace('Unchanged duty after the move', base=published['publication_id']))
    latest, = events(client.get(path))
    assert latest['UID'] == before['UID']
    assert latest[changed_field] == moved[changed_field]


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_legacy_feed_keeps_already_issued_uid_through_rollout_and_a_new_move(h):
    person = h.get('staff', '/api/me')['person_id']
    path = h.command('staff', 'calendar_feed.create', {})['path']

    def legacy_insert(mapper, connection, publication):
        publication.calendar_lineage = None

    event.listen(m.Publication, 'before_insert', legacy_insert)
    try:
        original = h.publish(h.shift(h.workspace(), people=[person])['workspace'])
        old = h.workspace('Legacy move', base=original['publication_id'])
        old = h.publish(move_duty(h, old, offset=timedelta(hours=1)))
    finally:
        event.remove(m.Publication, 'before_insert', legacy_insert)

    def legacy_events(result):
        with transaction(h.app.state.engine) as db:
            row = db.get(m.Publication, result['publication_id'])
            assert row.calendar_lineage is None
            return events(calendar_feed([(row.snapshot, row.sequence, row.created_at)], person, 'My duties'))

    original_event, = legacy_events(original)
    issued, = legacy_events(old)
    assert original_event['UID'] != issued['UID']
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        assert events(client.get(path)) == [issued]
        amendment = h.workspace('Move after rollout', base=old['publication_id'])
        h.publish(move_duty(h, amendment, offset=timedelta(hours=1)))
        current, = events(client.get(path))
    assert current['UID'] == issued['UID']
    assert current['DTSTART'] != issued['DTSTART']

    with pytest.raises(DBAPIError, match='immutable publication'), transaction(h.app.state.engine, write=True) as db:
        db.execute(text('UPDATE publications SET calendar_lineage = true WHERE id = :id'), {'id': old['publication_id']})


@pytest.mark.parametrize('metric_before', [None, 'scheduled-v1', METRIC_VERSION])
def test_copied_duplicate_session_keys_keep_uid_per_personal_reservation(h, subscription, metric_before):
    _, _, publication, _ = subscription
    me = h.get('staff', '/api/me')['person_id']
    snapshot = h.get('coordinator', '/api/publications/' + publication['publication_id'])['snapshot']
    first, = snapshot['shifts']
    second = deepcopy(first)
    second['id'] = 'second-session'
    first['participants'][0]['end'] = '2026-10-04T10:00:00+03:00'
    second['participants'][0]['start'] = '2026-10-04T10:00:00+03:00'
    snapshot['shifts'] = [first, second]
    # No presence gaps: each reservation is a single event, including for legacy snapshots.
    for shift in snapshot['shifts']:
        shift['participants'][0]['segments'] = []
    copied = deepcopy(snapshot)
    if metric_before is None:
        snapshot.pop('metric_version', None)
    else:
        snapshot['metric_version'] = metric_before
    copied['shifts'].reverse()
    for index, shift in enumerate(copied['shifts']):
        shift['id'] = f'copied-session-{index}'
    issued = '2026-10-03T06:00:00Z'
    before = events(calendar_feed([(snapshot, 1, issued)], me, 'My duties'))
    after = events(calendar_feed([(copied, 2, issued)], me, 'My duties'))

    def reservations(rows):
        return {(row['UID'], row['DTSTART'], row['DTEND']) for row in rows}

    assert len(before) == len(after) == 2
    assert len({event['UID'] for event in before}) == 2
    assert reservations(after) == reservations(before)
    original_ids = {shift['participants'][0]['start']: shift['id'] for shift in snapshot['shifts']}
    for shift in copied['shifts']:
        shift['lineage_id'] = original_ids[shift['participants'][0]['start']]
    copied['_calendar_duty_ids'] = publication_duty_ids(
        [(snapshot['shifts'], False, None), (copied['shifts'], True, None)], me)
    tracked = events(calendar_feed([(copied, 2, issued)], me, 'My duties'))
    assert len(tracked) == 2 and reservations(tracked) == reservations(before)


def test_amendment_uses_distinct_uids_for_a_moved_duty_and_a_new_duty_in_its_old_slot(h, subscription):
    _, _, publication, _ = subscription
    person = h.get('staff', '/api/me')['person_id']
    snapshot = h.get('coordinator', '/api/publications/' + publication['publication_id'])['snapshot']
    original, = snapshot['shifts']
    moved = {**original, 'id': 'moved-duty', 'lineage_id': original['id'], 'location_id': 'another-place'}
    new = {**original, 'id': 'new-duty', 'lineage_id': None}

    history = [([original], True, None)]
    before = publication_duty_ids(history, person)
    independent = publication_duty_ids([([new], True, None)], person)
    history.append(([moved, new], True, None))
    current = publication_duty_ids(history, person)
    assert current['moved-duty'] == before[original['id']]
    assert current['new-duty'] != current['moved-duty']
    assert independent['new-duty'] == current['new-duty']
    assert publication_duty_ids([*history, ([new], True, None)], person) == {
        'new-duty': current['new-duty'],
    }


def test_presence_time_edit_updates_the_existing_event(h, subscription):
    client, path, original, _ = subscription
    old, = events(client.get(path))
    amendment = h.workspace('Changed presence times', base=original['publication_id'])
    copied, = h.detail(amendment)['shifts']
    payload = {key: copied[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload['workspace_id'] = amendment['id']
    payload['shift_id'] = copied['id']
    participant, = copied['participants']
    other = next(p for p in h.seed['people'] if p != participant['person_id'])
    payload['participants'] = [
        {'person_id': participant['person_id'], 'start': participant['start'], 'end': participant['end'],
         'segments': [{'start': participant['start'], 'end': '2026-10-04T09:00:00+03:00'},
                      {'start': '2026-10-04T10:00:00+03:00', 'end': participant['end']}]},
        {'person_id': other, 'start': participant['start'], 'end': participant['end']}]
    edited = h.command('coordinator', 'shift.update', payload, revision=amendment['revision'])['workspace']
    h.publish(edited)
    current, later = events(client.get(path))
    assert current['UID'] == old['UID'] and current['DTEND'] != old['DTEND']
    assert later['UID'] != current['UID']


def test_replace_from_date_preserves_earlier_duty_uid_and_invalidates_etag(h, subscription):
    client, path, original, _ = subscription
    me = h.get('staff', '/api/me')['person_id']
    later_times = {'start': '2026-10-09T08:00:00+03:00', 'end': '2026-10-09T12:00:00+03:00'}
    extended = h.workspace('Include a duty after the replacement date', base=original['publication_id'])
    original = h.publish(h.shift(extended, people=[me], **later_times)['workspace'])
    first = client.get(path)
    old, future = events(first)
    replacement = h.command('coordinator', 'workspace.create', {
        'name': 'Replace from a date', 'starts_on': '2026-10-08', 'ends_on': '2026-10-15',
        'replaces_publication_id': original['publication_id']})['workspace']
    replacement = h.shift(replacement, people=[me], **later_times)['workspace']
    h.publish(replacement)
    response = client.get(path, headers={'If-None-Match': first.headers['etag']})
    assert response.status_code == 200 and response.headers['etag'] != first.headers['etag']
    earlier, later = events(response)
    assert earlier == old and later['UID'] != old['UID']
    assert later['UID'] == future['UID']  # same duty on a replaced day, recreated with fresh row IDs
    amended = h.publish(h.workspace('Amend partly replaced rota', base=original['publication_id']))
    earlier, later_again = events(client.get(path))
    assert earlier['UID'] == old['UID'] and earlier['SEQUENCE'] == str(amended['sequence'])
    assert later_again == later


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_replacement_reusing_a_moved_dutys_slot_keeps_both_identities(h):
    person = h.get('staff', '/api/me')['person_id']
    path = h.command('staff', 'calendar_feed.create', {})['path']
    replacement_start, replacement_end = '2026-10-06', '2026-10-11'
    slot = {'start': '2026-10-06T08:00:00+03:00', 'end': '2026-10-06T12:00:00+03:00'}
    original = h.publish(h.shift(h.workspace(), people=[person], **slot)['workspace'])
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        first, = events(client.get(path))
        amendment = h.workspace('Move into the retained range', base=original['publication_id'])
        amended = h.publish(move_duty(h, amendment, offset=timedelta(days=-2)))
        moved, = events(client.get(path))
        assert moved['UID'] == first['UID'] and moved['DTSTART'] < first['DTSTART']

        replacement = h.command('coordinator', 'workspace.create', {
            'name': 'Reuse the old slot', 'starts_on': replacement_start, 'ends_on': replacement_end,
            'replaces_publication_id': amended['publication_id'],
        })['workspace']
        replacement = h.publish(h.shift(replacement, people=[person], **slot)['workspace'])
        earlier, later = events(client.get(path))
        assert earlier == moved
        assert later['DTSTART'] == first['DTSTART'] and later['UID'] != earlier['UID']

        retained = h.workspace('Amend the retained duty', base=amended['publication_id'])
        retained = h.publish(move_duty(h, retained, offset=timedelta(hours=1)))
        new = h.workspace('Amend the replacement duty', start=replacement_start, end=replacement_end,
                          base=replacement['publication_id'])
        h.publish(move_duty(h, new, offset=timedelta(hours=1)))
        after = events(client.get(path))
        assert [duty['UID'] for duty in after] == [earlier['UID'], later['UID']]
        assert all(new_duty['DTSTART'] != old['DTSTART'] for new_duty, old in zip(after, (earlier, later)))

        removal = h.workspace('Remove the retained duty', base=retained['publication_id'])
        copied, = h.detail(removal)['shifts']
        removed = h.command('coordinator', 'shift.action', {'workspace_id': removal['id'],
            'shift_ids': [copied['id']], 'action': 'remove', 'reason': 'Remove the retained duty'},
            revision=removal['revision'])['workspace']
        other = next(ident for ident in h.seed['people'] if ident != person)
        h.publish(h.shift(removed, people=[other])['workspace'])
        assert events(client.get(path)) == [after[1]]


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_replacement_keeps_the_surviving_reservation_uid_in_a_shared_slot(h):
    person = h.get('staff', '/api/me')['person_id']
    path = h.command('staff', 'calendar_feed.create', {})['path']
    first_day, last_day = '2026-10-06', '2026-10-11'
    start, middle, end = ('2026-10-06T08:00:00+03:00', '2026-10-06T10:00:00+03:00',
                          '2026-10-06T12:00:00+03:00')

    def historical_bounds(mapper, connection, publication):
        # Older publications could store a reservation shorter than its session.
        publication.snapshot = {**publication.snapshot, 'shifts': [
            {**shift, 'start': start, 'end': end} for shift in publication.snapshot['shifts']
        ]}

    def publish_historical(draft):
        event.listen(m.Publication, 'before_insert', historical_bounds)
        try:
            return h.publish(draft)
        finally:
            event.remove(m.Publication, 'before_insert', historical_bounds)

    original = h.shift(h.workspace(), start, middle, people=[person])['workspace']
    original = publish_historical(h.shift(original, middle, end, people=[person])['workspace'])

    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        removed, kept = events(client.get(path))
        assert removed['DTEND'] == kept['DTSTART'] and removed['UID'] != kept['UID']
        replacement = h.command('coordinator', 'workspace.create', {
            'name': 'Keep the later reservation', 'starts_on': first_day, 'ends_on': last_day,
            'replaces_publication_id': original['publication_id'],
        })['workspace']
        published = publish_historical(h.shift(replacement, middle, end, people=[person])['workspace'])
        current, = events(client.get(path))
        assert current['UID'] == kept['UID']
        assert (current['DTSTART'], current['DTEND']) == (kept['DTSTART'], kept['DTEND'])

        amendment = h.workspace('Amend retained reservation', start=first_day, end=last_day,
                                base=published['publication_id'])
        copied, = h.detail(amendment)['shifts']
        payload = {key: copied[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'attributes')}
        payload.update(workspace_id=amendment['id'], shift_id=copied['id'], start=middle, end=end,
                       participants=[{'person_id': person, 'start': middle, 'end': end}])
        edited = h.command('coordinator', 'shift.update', payload, revision=amendment['revision'])['workspace']
        h.publish(edited)
        amended, = events(client.get(path))
        assert amended['UID'] == kept['UID']


def test_new_publication_changes_etag(h, subscription):
    client, path, _, _ = subscription
    first = client.get(path)
    me = h.get('staff', '/api/me')['person_id']
    w = h.workspace('Next rota', start='2026-10-11', end='2026-10-18')
    w = h.shift(w, start='2026-10-12T08:00:00+03:00', end='2026-10-12T12:00:00+03:00', people=[me])['workspace']
    h.publish(w)
    response = client.get(path, headers={'If-None-Match': first.headers['etag']})
    assert response.status_code == 200 and response.headers['etag'] != first.headers['etag']
    assert len(events(response)) == 2


def test_calendar_name_change_does_not_change_content_etag(h, subscription):
    client, path, _, _ = subscription
    first = client.get(path)
    me = h.get('staff', '/api/me')['person_id']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, me).display_name = 'Renamed person (test)'
    response = client.get(path, headers={'If-None-Match': first.headers['etag']})
    assert response.status_code == 304 and response.headers['etag'] == first.headers['etag']
    assert 'Renamed person (test)' not in response.text


def test_personal_ics_export_uses_department_title_without_person_name(h, subscription):
    _, _, publication, _ = subscription
    me = h.get('staff', '/api/me')['person_id']
    person_name = h.get('coordinator', '/api/publications/' + publication['publication_id'])['snapshot']['people'][me]['display_name']
    department_name = h.get('staff', '/api/me')['department']['name']
    response = h.client('staff').get('/api/reports/' + publication['publication_id'] + '?format=ics')
    assert response.status_code == 200
    text = response.text.replace('\r\n ', '')
    assert f'X-WR-CALNAME:{department_name} rota' in text
    assert person_name not in text
    assert 'Published personal rota' not in text


def test_date_adjustment_updates_live_schedule_but_preserves_both_published_ics_exports(h):
    holiday = h.configure('date_sets', {'name': 'Public holidays', 'entries': [{
        'name': 'Synthetic Eid', 'first_day': '2026-10-04', 'last_day': '2026-10-05',
        'repeat': 'yearly_gregorian'}]})['record']
    me = h.get('staff', '/api/me')['person_id']
    publication = h.publish(h.shift(h.workspace(), people=[me])['workspace'])
    publication_path = '/api/publications/' + publication['publication_id']
    frozen = h.get('coordinator', publication_path)['snapshot']['date_sets']
    path = h.command('staff', 'calendar_feed.create', {})['path']
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        before = client.get(path)
        h.command('admin', 'date_set.adjust', {
            'entry_id': holiday['entries'][0]['id'], 'year': 2026,
            'override': {'year': 2026, 'first_day': '2026-10-05', 'last_day': '2026-10-06'},
            'reason': 'Synthetic Eid adjustment'}, holiday['id'], holiday['revision'])
        cached = client.get(path, headers={'If-None-Match': before.headers['etag']})
        refreshed = client.get(path)
    assert cached.status_code == 304
    assert refreshed.status_code == 200 and refreshed.headers['etag'] == before.headers['etag']
    schedule = h.get('staff', '/api/schedule/days?start=2026-10-04&end=2026-10-07')
    assert set(schedule['date_set_markers']) == {'2026-10-05', '2026-10-06'}
    static = h.client('staff').get('/api/reports/' + publication['publication_id'] + '?format=ics')
    assert static.status_code == 200
    for response in (refreshed, static):
        rows = events(response)
        assert [row['DTSTART;VALUE=DATE'] for row in rows if 'DTSTART;VALUE=DATE' in row] == [
            '20261004', '20261005']
        duty, = [row for row in rows if 'DTSTART' in row]
        assert 'Synthetic Eid' in duty['X-FMUC-DATE-SETS']
        assert 'Synthetic Eid' in duty['DESCRIPTION']
        assert duty['DTSTART'] == next(row for row in events(before) if 'DTSTART' in row)['DTSTART']
    assert h.get('coordinator', publication_path)['snapshot']['date_sets'] == frozen


@pytest.mark.parametrize('ceiling', ['rate_feed_per_hour', 'rate_feed_address_per_hour'])
def test_conditional_requests_still_obey_both_rate_limits(h, subscription, monkeypatch, ceiling):
    client, path, _, _ = subscription
    freeze_clock(monkeypatch)
    object.__setattr__(h.settings, ceiling, 1)
    first = client.get(path)
    assert first.status_code == 200
    response = client.get(path, headers={'If-None-Match': first.headers['etag']})
    assert response.status_code == 429 and response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('lost_access', ['disabled', 'unbound'])
def test_conditional_request_checks_access_before_the_etag(h, subscription, lost_access):
    client, path, _, _ = subscription
    first = client.get(path)
    identity_id = h.get('staff', '/api/me')['identity_id']
    with transaction(h.app.state.engine, write=True) as db:
        identity = db.get(m.Identity, identity_id)
        if lost_access == 'disabled':
            identity.disabled = True
        else:
            identity.person_id = None
    assert client.get(path, headers={'If-None-Match': first.headers['etag']}).status_code == 404


def test_large_feed_etag_identifies_the_same_bytes_for_gzip_and_identity(h):
    me = h.get('staff', '/api/me')['person_id']
    w = h.workspace()
    for day in (4, 5, 6):
        w = h.shift(w, start=f'2026-10-{day:02}T08:00:00+03:00',
                    end=f'2026-10-{day:02}T12:00:00+03:00', people=[me])['workspace']
    h.publish(w)
    path = h.command('staff', 'calendar_feed.create', {})['path']
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        compressed = client.get(path, headers={'Accept-Encoding': 'gzip'})
        plain = client.get(path, headers={'Accept-Encoding': 'identity'})
        assert compressed.status_code == plain.status_code == 200
        assert len(compressed.content) > 1024
        assert 'content-encoding' not in compressed.headers
        assert compressed.content == plain.content
        assert compressed.headers['etag'] == plain.headers['etag']
