"""Immediate Undo retracts queued notices; retained and already delivered evidence remains intact."""
from .clock_support import freeze_clock
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import retry_write, transaction
from apps.api.services import common
from apps.worker import main as worker
from packages.domain.time import aware
from tests.test_request_decide_many import decide, request_row


def events(h, prefix):
    with transaction(h.app.state.engine) as db:
        return [(row.event_key, row.state, row.attempts, aware(row.next_attempt_at))
                for row in db.scalars(select(m.Outbox).where(m.Outbox.event_key.startswith(prefix)))]


def absence(h):
    return h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0],
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T10:00:00+03:00',
        'kind': 'actual_absence', 'private_reason': 'Synthetic absence'})['record']


def undo(h, command):
    return h.command('coordinator', command['kind'], command['payload'],
                     command.get('target_id'), command.get('expected_revision'))


def test_outbox_holds_reversible_notification_then_dispatches_exactly_when_due(h, monkeypatch):
    before = m.now()
    freeze_clock(monkeypatch, lambda: before)
    row = absence(h)
    event, = events(h, 'availability:' + row['id'])
    assert event[3] == before + timedelta(seconds=common.UNDO_GRACE_SECONDS)
    assert retry_write(h.app.state.engine, worker.dispatch_batch) == set()
    freeze_clock(monkeypatch, lambda: before + timedelta(seconds=common.UNDO_GRACE_SECONDS))
    assert retry_write(h.app.state.engine, worker.dispatch_batch) == {h.seed['department_id']}
    assert events(h, event[0])[0][1:3] == ('available_in_app', 1)


def test_record_then_immediate_retract_supersedes_notice_without_erasing_evidence(h):
    row = absence(h)
    h.command('coordinator', 'availability.retract', {'reason': 'Undo'}, row['id'], row['revision'])
    assert events(h, 'availability:' + row['id'])[0][1] == 'superseded'
    assert events(h, 'availability-retracted:' + row['id']) == []
    assert retry_write(h.app.state.engine, worker.dispatch_batch) == set()


def test_availability_update_undo_and_retract_restore_cancel_their_exact_pending_versions(h):
    row = absence(h)
    changed = h.command('coordinator', 'availability.update', {'end': '2026-10-04T11:00:00+03:00', 'reason': 'Extend'},
        row['id'], row['revision'])
    undo(h, changed['undo'])
    assert events(h, f"availability-updated:{row['id']}:{changed['record']['revision']}")[0][1] == 'superseded'
    with transaction(h.app.state.engine, write=True) as db:
        for event in db.scalars(select(m.Outbox)):
            event.state = 'available_in_app'
    current = h.get('coordinator', '/api/availability')['availability'][0]
    removed = h.command('coordinator', 'availability.retract', {'reason': 'Remove'}, current['id'], current['revision'])
    undo(h, removed['undo'])
    assert events(h, f"availability-retracted:{row['id']}")[0][1] == 'superseded'
    assert events(h, f"availability-restored:{row['id']}") == []


@pytest.mark.parametrize('many', [False, True])
def test_single_and_batch_request_undo_cancel_pending_decisions(h, many):
    rows = [request_row(h)]
    if many:
        rows.append(request_row(h, kind='meeting'))
        changed = decide(h, rows)
        h.command('approver', 'request.undo', {'batch_id': changed['batch_id'], 'reason': 'Undo'})
    else:
        row = rows[0]
        changed = h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Arrange cover'}, row['id'], row['revision'])
        row = changed['record']
        h.command('approver', 'request.undo', {'reason': 'Undo'}, row['id'], row['revision'])
    for row in rows:
        assert events(h, 'request-decision:' + row['id'])[0][1] == 'superseded'
        assert events(h, 'request-undo:' + row['id']) == []


def test_request_withdrawal_and_pattern_end_retract_the_undelivered_request(h):
    row = request_row(h)
    h.command('staff', 'request.decide', {'decision': 'withdraw', 'reason': 'Withdraw'}, row['id'], row['revision'])
    assert events(h, 'request:' + row['id'])[0][1] == 'superseded'
    assert events(h, 'request-decision:' + row['id']) == []
    pattern = h.command('staff', 'availability_pattern.propose', {'person_id': h.seed['people'][0], 'weekdays': [1],
        'start_time': '13:00', 'end_time': '17:00', 'starts_on': '2026-10-01'})['pattern']
    h.command('staff', 'availability_pattern.end', {'reason': 'Withdraw'}, pattern['id'], pattern['revision'])
    assert events(h, 'availability-pattern:' + pattern['id'])[0][1] == 'superseded'


def test_approved_leave_withdrawal_reaches_approver_when_submission_is_queued(h, monkeypatch):
    before = freeze_clock(monkeypatch)
    row = request_row(h)
    approved = h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Confirm leave'},
                         row['id'], row['revision'])['record']
    submission = f"request:{row['id']}"
    assert events(h, submission)[0][1] == 'queued'

    withdrawn = h.command('staff', 'request.decide', {'decision': 'withdraw', 'reason': 'Plans changed'},
                          approved['id'], approved['revision'])['record']
    withdrawal = f"request-decision:{row['id']}:{withdrawn['revision']}"
    assert events(h, submission)[0][1] == 'superseded'
    assert events(h, withdrawal)[0][1] == 'queued'

    freeze_clock(monkeypatch, before + timedelta(seconds=common.UNDO_GRACE_SECONDS))
    assert retry_write(h.app.state.engine, worker.dispatch_batch) == {h.seed['department_id']}
    assert events(h, withdrawal)[0][1] == 'available_in_app'
    notices = h.get('approver', '/api/notifications')['notifications']
    assert sum(note['title'].endswith('leave request was withdrawn') for note in notices) == 1
    assert h.get('staff_two', '/api/notifications')['notifications'] == []


def test_feedback_undo_supersedes_the_exact_status_notice(h):
    from tests.test_feedback_api import detail, submit, update
    report = detail(h, submit(h)['id'])
    changed = update(h, report, status='fixed')['report']
    h.command('operator', 'feedback.undo', {'id': changed['id'], 'revision': changed['revision']})
    assert events(h, f"feedback-status:{report['id']}:{changed['revision']}")[0][1] == 'superseded'
    assert len(events(h, f"feedback-status:{report['id']}")) == 1


def test_swap_undo_preserves_forward_consent_without_sending_the_undelivered_step(h, monkeypatch):
    from tests import test_requests_unified as requests
    monkeypatch.setattr(requests, 'NOW', m.now() - timedelta(hours=12))
    _, second, offered = requests.swap_fixture(h)
    changed = requests.swap_step(h, 'staff_two', 'counteroffer', offered, {'assignment_id': second, 'reason': 'Offer in return'})
    reopened = requests.swap_step(h, 'staff_two', 'undo', changed, {'reason': 'Undo'})
    assert reopened['status'] == 'offered' and reopened['id'] != offered['id']
    assert events(h, f"swap:{offered['id']}:{changed['revision']}:counteroffer")[0][1] == 'superseded'
    assert events(h, 'swap:' + reopened['id'])[0][1] == 'superseded'


def test_inverse_never_rewrites_delivered_or_foreign_department_events(h):
    actor = NS(department_id=h.seed['department_id'], identity_id=h.seed['identities']['coordinator'])
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Other synthetic department')
        db.add(other)
        db.flush()
        db.add_all([m.Outbox(department_id=actor.department_id, event_key='delivered-source', kind='availability',
            payload={}, state='available_in_app'), m.Outbox(department_id=other.id, event_key='foreign-source',
            kind='availability', payload={})])
    with transaction(h.app.state.engine, write=True) as db:
        assert common.outbox(db, actor, 'inverse', 'availability', {}, undo_of=['delivered-source', 'foreign-source'])
    assert events(h, 'delivered-source')[0][1] == 'available_in_app'
    assert events(h, 'foreign-source')[0][1] == 'queued'
    assert events(h, 'inverse')[0][1] == 'queued'


def test_catalogue_history_counter_rolls_back_and_retries_without_session_memos(h):
    actor = NS(department_id=h.seed['department_id'])
    with transaction(h.app.state.engine, write=True) as db:
        before = db.scalar(select(m.Department.catalogue_revision).where(m.Department.id == actor.department_id))
        with pytest.raises(RuntimeError):
            with db.begin_nested():
                common.history_many(db, actor, [])
                raise RuntimeError('Synthetic savepoint rollback')
        assert db.scalar(select(m.Department.catalogue_revision).where(m.Department.id == actor.department_id)) == before
        common.history_many(db, actor, [])
        assert db.scalar(select(m.Department.catalogue_revision).where(m.Department.id == actor.department_id)) == before + 1


def test_catalogue_counter_does_not_flush_deferred_bulk_records(h, monkeypatch):
    actor = NS(department_id=h.seed['department_id'], identity_id=h.seed['identities']['admin'])
    with transaction(h.app.state.engine, write=True) as db:
        db.info['bulk'] = {'history': []}
        pending = m.Category(department_id=actor.department_id, name='Deferred synthetic rank', starts_on=m.now().date())
        db.add(pending)
        def unexpected_flush(*args, **kwargs):
            raise AssertionError('Catalogue invalidation must preserve deferred bulk flushes')
        with monkeypatch.context() as patch:
            patch.setattr(db, 'flush', unexpected_flush)
            common.history_many(db, actor, [('categories', pending, 'Synthetic batch')])
            assert pending in db.new and len(db.info['bulk']['history']) == 1


def test_pending_and_dirty_superseded_sources_are_resolved_without_flushing_bulk(h):
    actor = NS(department_id=h.seed['department_id'], identity_id=h.seed['identities']['coordinator'])
    with transaction(h.app.state.engine, write=True) as db:
        with db.no_autoflush:
            source = common.outbox(db, actor, 'pending-source', 'availability', {}, undoable=True)
            assert not common.outbox(db, actor, 'inverse-not-sent', 'availability', {}, undo_of='pending-source')
            assert source in db.new and source.state == 'superseded'
            assert common.outbox(db, actor, 'inverse-again', 'availability', {}, undo_of='pending-source')
        common.outbox(db, actor, 'persisted-source', 'availability', {}, undoable=True)
    with transaction(h.app.state.engine, write=True) as db:
        with db.no_autoflush:
            assert common.supersede_outbox(db, actor, 'persisted-source')
            assert not common.supersede_outbox(db, actor, 'persisted-source')


def test_record_update_update_retract_cancels_every_undispatched_version(h):
    row = absence(h)
    for hour in (11, 12):
        row = h.command('coordinator', 'availability.update', {'end': f'2026-10-04T{hour}:00:00+03:00', 'reason': 'Adjust'},
            row['id'], row['revision'])['record']
    h.command('coordinator', 'availability.retract', {'reason': 'Remove'}, row['id'], row['revision'])
    with transaction(h.app.state.engine) as db:
        rows = list(db.scalars(select(m.Outbox).where(m.Outbox.payload['availability_id'].as_string() == row['id'])))
        assert len(rows) == 3 and all(event.state == 'superseded' for event in rows)


@pytest.mark.parametrize('old_retry', [False, True])
def test_retract_notifies_when_an_active_version_was_delivered_even_with_pending_history(h, old_retry):
    from tests.outbox_support import process_one_after_undo
    row = absence(h)
    if old_retry:
        with transaction(h.app.state.engine, write=True) as db:
            original = db.scalar(select(m.Outbox).where(m.Outbox.event_key == 'availability:' + row['id']))
            original.state, original.attempts = 'retry', 1
            original.next_attempt_at = m.now() + timedelta(minutes=1)
        row = h.command('coordinator', 'availability.update', {'end': '2026-10-04T11:00:00+03:00', 'reason': 'Adjust'},
            row['id'], row['revision'])['record']
    process_one_after_undo(h.app.state.engine)
    row = h.command('coordinator', 'availability.update', {'end': '2026-10-04T12:00:00+03:00', 'reason': 'Adjust again'},
        row['id'], row['revision'])['record']
    removed = h.command('coordinator', 'availability.retract', {'reason': 'Remove'}, row['id'], row['revision'])['record']
    key = f"availability-retracted:{row['id']}:{removed['revision']}"
    assert events(h, key)[0][1] == 'queued'
    process_one_after_undo(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        titles = list(db.scalars(select(m.Notification.title).join(m.Outbox).where(m.Outbox.event_key == key)))
        assert titles and all('removed' in title for title in titles)


def test_ended_proposal_restores_a_new_revision_notice_then_can_be_withdrawn_again(h):
    pattern = h.command('staff', 'availability_pattern.propose', {'person_id': h.seed['people'][0], 'weekdays': [1],
        'start_time': '13:00', 'end_time': '17:00', 'starts_on': '2026-10-01'})['pattern']
    ended = h.command('staff', 'availability_pattern.end', {'reason': 'Withdraw'}, pattern['id'], pattern['revision'])['pattern']
    restored = h.command('staff', 'availability_pattern.restore', {'reason': 'Restore'}, ended['id'], ended['revision'])['pattern']
    restored_key = f"availability-pattern:{pattern['id']}:{restored['revision']}:restored"
    assert restored['status'] == 'proposed' and events(h, restored_key)[0][1] == 'queued'
    assert events(h, 'availability-pattern:' + pattern['id'])[0][1] == 'superseded'
    h.command('staff', 'availability_pattern.end', {'reason': 'Withdraw again'}, restored['id'], restored['revision'])
    assert events(h, restored_key)[0][1] == 'superseded'


def test_explicit_delivery_fixture_preserves_future_schedules_and_retry_deadlines(h):
    from tests.outbox_support import mature_undo_notifications
    actor = NS(department_id=h.seed['department_id'], identity_id=h.seed['identities']['coordinator'])
    with transaction(h.app.state.engine, write=True) as db:
        held = common.outbox(db, actor, 'held', 'availability', {}, undoable=True)
        future = common.outbox(db, actor, 'future', 'announcement', {}, at=m.now() + timedelta(days=1), undoable=True)
        retry = common.outbox(db, actor, 'retry', 'availability', {}, undoable=True)
        retry.state, retry.attempts = 'retry', 1
        before = (future.next_attempt_at, retry.next_attempt_at)
        mature_undo_notifications(db)
        assert aware(held.next_attempt_at) <= m.now()
        assert (future.next_attempt_at, retry.next_attempt_at) == before
