"""People-view persistence and receipt-bound Undo for mark-all-read."""
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from packages.contracts import schemas as s
from .governance_support import identity
from .test_schedule_views_api import listing, save, views


@pytest.mark.parametrize('sort', ['hours_desc', 'hours_asc', 'name'])
def test_people_view_contract_preserves_independent_sorts(sort):
    for group in ('rank', 'tag', 'team', 'none'):
        settings = s.PlannerViewSettings(mode='person', group_by=group, people_sort=sort, sort_by='code')
        assert settings.model_dump(mode='json')['people_sort'] == sort
        assert settings.group_by == group and settings.sort_by == 'code'
    for old in ({}, {'mode': 'month', 'sort_by': 'time'}, {'mode': 'timeline', 'sort_by': 'hours'}):
        settings = s.PlannerViewSettings.model_validate(old)
        assert settings.people_sort is None and 'people_sort' not in settings.model_dump(mode='json')
    for bad in ({'people_sort': 'hours-desc'}, {'people_sort': 'hours'}, {'group_by': 'unknown'}):
        with pytest.raises(ValidationError):
            s.PlannerViewSettings.model_validate(bad)


def test_both_people_view_apis_round_trip_and_keep_privacy(h, views):
    settings = {'mode': 'person', 'group_by': 'team', 'people_sort': 'hours_desc'}
    row = save(h, views, settings=settings)['view']
    definition = listing(h, views)[0]['definition']
    stored = definition['planner'] if views == 'table' else definition
    assert all(stored[key] == value for key, value in settings.items())
    assert listing(h, views, 'approver') == []
    save(h, views, 'approver', action='update', row=row, settings=settings, status=404)
    for sort in ('hours_asc', 'name'):
        settings['people_sort'] = sort
        row = save(h, views, action='update', row=row, settings=settings)['view']
        definition = listing(h, views)[0]['definition']
        assert (definition['planner'] if views == 'table' else definition)['people_sort'] == sort
    shared = save(h, views, scope='department', settings=settings)['view']
    assert any(item['id'] == shared['id'] and not item['can_edit'] for item in listing(h, views, 'approver'))
    save(h, views, 'approver', scope='department', settings=settings, status=403)
    for personal in ({'person_id': h.seed['people'][0]}, {'filters': {'person_ids': [h.seed['people'][0]]}}):
        save(h, views, scope='department', settings={**settings, **personal}, status=422)
        save(h, views, action='update', row=shared, scope='department', settings={**settings, **personal}, status=422)


def test_pre_people_sort_saved_views_load_and_can_be_saved_again(h, views):
    row = save(h, views)['view']
    old = {'mode': 'month', 'group_by': 'rank', 'sort_by': 'time'}
    with transaction(h.app.state.engine, write=True) as db:
        model = m.TableView if views == 'table' else m.PlannerView
        db.get(model, row['id']).definition = {'planner': old} if views == 'table' else old
    [loaded] = listing(h, views)
    stored = loaded['definition']['planner'] if views == 'table' else loaded['definition']
    assert stored == old
    assert s.PlannerViewSettings.model_validate(stored).people_sort is None
    saved = save(h, views, action='update', row=loaded, settings=stored)['view']
    stored = saved['definition']['planner'] if views == 'table' else saved['definition']
    assert 'people_sort' not in stored and stored['group_by'] == 'rank'


def add_note(h, *, role='staff', department=None, **extra):
    with transaction(h.app.state.engine, write=True) as db:
        outbox = m.Outbox(department_id=department or h.seed['department_id'], event_key=m.uid(), kind='publication', payload={})
        db.add(outbox)
        db.flush()
        note = m.Notification(outbox_id=outbox.id, identity_id=h.seed['identities'][role],
                              title='Synthetic notification', message='Synthetic inbox item', **extra)
        db.add(note)
        db.flush()
        return record(note)


def note_state(h, note_id):
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Notification, note_id)
        return record(row)


def undo(h, reply, *, role='staff', status=200, key=None):
    command = reply['undo']
    return h.command(role, command['kind'], command['payload'], status=status, key=key)


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_mark_all_undo_round_trip_is_own_only_and_audited_for_every_role(h, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    notes = [add_note(h, role=role), add_note(h, role=role, source_kind='publication')]
    earlier = m.now() - timedelta(days=1)
    already_read = add_note(h, role=role, read_at=earlier, acknowledged_at=earlier)
    foreign_role = 'staff' if role != 'staff' else 'coordinator'
    foreign = add_note(h, role=foreign_role)
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Other synthetic department')
        db.add(department)
        db.flush()
        other_department = department.id
    foreign_department = add_note(h, role=role, department=other_department)
    result = h.command(role, 'notification.ack_all')
    s.NotificationReadResult.model_validate(result)
    assert set(result['notification_ids']) == {row['id'] for row in notes}
    assert result['read'] == result['acknowledged'] == 2 and result['unread_total'] == 0
    assert result['undo']['payload']['undo_of'] == result['receipt_id']
    undo(h, result, role=foreign_role, status=404)
    restored = undo(h, result, role=role)
    s.NotificationReadResult.model_validate(restored)
    assert restored['unread'] == restored['unread_total'] == 2
    for before in notes:
        after = note_state(h, before['id'])
        assert after == {**before, 'revision': 3}
    for before in (already_read, foreign, foreign_department):
        assert note_state(h, before['id']) == before
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.actor_id == h.seed['identities'][role],
            m.AuditEvent.action.in_(['notification.ack_all', 'notification.unack']))))
        assert {event.action for event in events} == {'notification.ack_all', 'notification.unack'}
        assert {event.detail['receipt_id'] for event in events} == {result['receipt_id'], restored['receipt_id']}
    assert s.AUDIT_ACTION_CATALOGUE['notification.unack']['verb'] == 'undid reading'


def test_mark_all_undo_preserves_prior_read_and_acknowledgement_evidence(h):
    earlier = m.now() - timedelta(days=1)
    notes = [add_note(h, read_at=earlier), add_note(h, acknowledged_at=earlier)]
    result = h.command('staff', 'notification.ack_all')
    assert set(result['notification_ids']) == {row['id'] for row in notes}
    restored = undo(h, result)
    assert restored['unread'] == 1
    for before in notes:
        assert note_state(h, before['id']) == {**before, 'revision': 3}


def test_mark_all_undo_is_idempotent_and_does_not_unread_later_reads(h):
    note = add_note(h)
    result = h.command('staff', 'notification.ack_all', key='be2-mark-all-read-once')
    assert h.command('staff', 'notification.ack_all', key='be2-mark-all-read-once') == result
    assert h.command('staff', 'notification.ack_all')['undo'] is None
    restored = undo(h, result, key='be2-undo-mark-all-once')
    assert undo(h, result, key='be2-undo-mark-all-once') == restored
    assert undo(h, result)['unread'] == 0
    assert note_state(h, note['id'])['revision'] == 3
    h.command('staff', 'notification.ack_all')
    assert undo(h, result)['unread'] == 0
    assert note_state(h, note['id'])['read_at']


def test_mark_all_undo_refuses_tampered_lists_and_stale_changes_atomically(h):
    first, second = add_note(h), add_note(h)
    result = h.command('staff', 'notification.ack_all')
    payload = result['undo']['payload']
    foreign = add_note(h, role='coordinator')
    for ids in ([first['id']], [foreign['id']]):
        reply = h.command('staff', 'notification.unack', {**payload, 'notification_ids': ids}, status=409)
        assert reply['error']['code'] == 'NOTIFICATION_UNDO_MISMATCH'
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Notification, second['id']).revision += 1
    stale = undo(h, result, status=409)
    assert stale['error']['code'] == 'STALE_REVISION'
    assert note_state(h, first['id'])['read_at'] and note_state(h, first['id'])['revision'] == 2


@pytest.mark.parametrize('source', ['announcement', 'event'])
def test_mark_all_undo_restores_shared_receipts_and_preserves_explicit_ack(h, source):
    if source == 'announcement':
        from .test_announcement_receipts import notice, process_one
        content = notice(h, {'roles': ['staff']}, importance='must_acknowledge', notify=True)
        source_id, source_kind = content['id'], 'department_content'
    else:
        from .test_event_delivery import scheduled, process_one
        _, occurrences = scheduled(h, attendance='must_acknowledge')
        source_id, source_kind = occurrences[0], 'event'
    process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        receipt = record(db.scalar(select(m.AnnouncementReceipt).where(
            m.AnnouncementReceipt.identity_id == h.seed['identities']['staff'],
            (m.AnnouncementReceipt.content_id == source_id) if source == 'announcement'
            else (m.AnnouncementReceipt.event_occurrence_id == source_id))))
    add_note(h, source_kind=source_kind, source_id=source_id)
    result = h.command('staff', 'notification.ack_all')
    assert len(result['notification_ids']) == 2 and len(result['previous_receipts']) == 1
    undo(h, result)
    with transaction(h.app.state.engine) as db:
        assert record(db.get(m.AnnouncementReceipt, receipt['id'])) == {**receipt, 'revision': receipt['revision'] + 2}
    assert h.get('staff', '/api/notifications')['unread_total'] == 2
    # Explicit acknowledgement after mark-all-read invalidates Undo without erasing consent evidence.
    result = h.command('staff', 'notification.ack_all')
    note = h.get('staff', '/api/notifications')['notifications'][0]
    h.command('staff', 'notification.acknowledge', {'reason': 'Explicit synthetic acknowledgement'}, note['id'], note['revision'])
    undo(h, result, status=409)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.AnnouncementReceipt, receipt['id']).acknowledged_at


def test_notification_unack_contract_and_api_reject_invalid_or_unbounded_ids(h):
    payload = {'notification_ids': [m.uid()], 'undo_of': m.uid()}
    s.NotificationUnack.model_validate(payload)
    s.NotificationUnack.model_validate({**payload, 'notification_ids': [m.uid() for _ in range(s.NOTIFICATION_UNDO_LIMIT)]})
    for bad in ({'notification_ids': ['invalid']}, {'notification_ids': payload['notification_ids'] * 2},
                {'notification_ids': [m.uid() for _ in range(s.NOTIFICATION_UNDO_LIMIT + 1)]},
                {'undo_of': 'invalid'}, {'reason': ''}, {'unexpected': True}):
        value = {**payload, **bad}
        with pytest.raises(ValidationError):
            s.NotificationUnack.model_validate(value)
        assert h.command('staff', 'notification.unack', value, status=422)['error']['code'] == 'SCHEMA_INVALID'
    h.command('staff', 'notification.unack', payload, status=404)
    h.command('staff', 'notification.unack', {'notification_ids': []}, status=422)


def test_mark_all_bound_fails_before_writing_any_notification(h):
    with transaction(h.app.state.engine, write=True) as db:
        outboxes = [{'id': m.uid(), 'department_id': h.seed['department_id'], 'event_key': m.uid(),
                     'kind': 'publication', 'payload': {}} for _ in range(s.NOTIFICATION_UNDO_LIMIT + 1)]
        db.execute(insert(m.Outbox), outboxes)
        db.execute(insert(m.Notification), [{'outbox_id': row['id'], 'identity_id': h.seed['identities']['staff'],
            'title': 'Synthetic capacity item', 'message': 'Bounded Undo'} for row in outboxes])
    result = h.command('staff', 'notification.ack_all', status=409)
    assert result['error']['code'] == 'NOTIFICATION_UNDO_LIMIT'
    assert h.get('staff', '/api/notifications')['unread_total'] == s.NOTIFICATION_UNDO_LIMIT + 1


def test_mark_all_undo_can_restore_a_receipt_without_unreading_previously_read_notes(h):
    from .test_announcement_receipts import notice, process_one
    content = notice(h, {'roles': ['staff']}, notify=True)
    process_one(h.app.state.engine)
    with transaction(h.app.state.engine, write=True) as db:
        note = db.scalar(select(m.Notification).where(m.Notification.source_id == content['id']))
        note.read_at = m.now() - timedelta(days=1)
    result = h.command('staff', 'notification.ack_all')
    assert result['notification_ids'] == [] and result['previous_receipts']
    assert undo(h, result)['unread'] == 0
    assert h.get('staff', '/api/notifications')['unread_total'] == 0
