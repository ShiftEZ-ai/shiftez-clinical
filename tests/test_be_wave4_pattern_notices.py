"""Ending a pattern cancels every undelivered request revision and retains delivered evidence."""
from .clock_support import freeze_clock
from datetime import timedelta

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from packages.domain.time import aware
from tests.outbox_support import process_one_after_undo
from tests.test_availability_patterns import pattern


def notice(h, key):
    with transaction(h.app.state.engine) as db:
        row = db.scalar(select(m.Outbox).where(m.Outbox.event_key == key))
        notifications = [record(item) for item in db.scalars(select(m.Notification).where(
            m.Notification.outbox_id == row.id).order_by(m.Notification.id))]
        return record(row), notifications


@pytest.mark.parametrize('delivery', ['queued', 'retry', 'delivered'])
def test_end_after_restored_proposal_approval_cancels_all_pending_revisions_and_undo_preserves_decision(h, monkeypatch, delivery):
    person = h.get('staff', '/api/me')['person_id']
    proposed = h.command('staff', 'availability_pattern.propose', pattern(person))['pattern']
    ended = h.command('staff', 'availability_pattern.end', {'reason': 'Withdraw proposal'},
                      proposed['id'], proposed['revision'])['pattern']
    restored = h.command('staff', 'availability_pattern.restore', {'reason': 'Restore proposal'},
                         ended['id'], ended['revision'])['pattern']
    key = f"availability-pattern:{proposed['id']}:{restored['revision']}:restored"
    other = h.command('staff', 'availability_pattern.propose', pattern(person, weekdays=[2]))['pattern']
    other_key = 'availability-pattern:' + other['id']
    if delivery == 'retry':
        with transaction(h.app.state.engine, write=True) as db:
            event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == key))
            event.state, event.attempts = 'retry', 1
            event.next_attempt_at = m.now() + timedelta(minutes=1)
    elif delivery == 'delivered':
        process_one_after_undo(h.app.state.engine)
    before, evidence = notice(h, key)
    assert before['state'] == ('available_in_app' if delivery == 'delivered' else delivery)
    assert bool(evidence) == (delivery == 'delivered')
    other_before = notice(h, other_key)
    approved = h.command('coordinator', 'availability_pattern.decide', {'decision': 'approve', 'reason': 'Independent review'},
                         restored['id'], restored['revision'])['pattern']
    freeze_clock(monkeypatch, lambda: aware(approved['decided_at']) + timedelta(hours=1))
    ended = h.command('staff', 'availability_pattern.end', {'reason': 'End approved pattern'},
                      approved['id'], approved['revision'])['pattern']
    assert [proposed['revision'], restored['revision'], approved['revision'], ended['revision']] == [1, 3, 4, 5]
    expected = {**before, 'state': 'available_in_app' if delivery == 'delivered' else 'superseded'}
    assert notice(h, key) == (expected, evidence)
    assert notice(h, other_key) == other_before

    undone = h.command('staff', 'availability_pattern.restore', {'reason': 'Undo ending'},
                       ended['id'], ended['revision'])['pattern']
    assert undone['revision'] == 6 and undone['status'] == 'active'
    assert (undone['decided_by'], undone['decided_at']) == (approved['decided_by'], approved['decided_at'])
    assert notice(h, key) == (expected, evidence)
    assert notice(h, other_key) == other_before
