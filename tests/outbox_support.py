"""Explicit post-Undo-window fixture delivery; future schedules and retries keep their deadlines."""
from datetime import timedelta
from sqlalchemy import update

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.common import UNDO_GRACE_SECONDS
from apps.worker import main as worker


def mature_undo_notifications(db):
    """These tests exercise delivery after the hold, without sleeping or changing lease/calendar clocks."""
    moment = m.now()
    db.execute(update(m.Outbox).where(m.Outbox.state == 'queued', m.Outbox.attempts == 0,
        m.Outbox.kind.in_(['availability', 'request', 'swap', 'feedback', 'announcement', 'event']),
        m.Outbox.next_attempt_at > moment,
        m.Outbox.next_attempt_at <= moment + timedelta(seconds=UNDO_GRACE_SECONDS))
        .values(next_attempt_at=moment))


def process_one_after_undo(engine, *args, **kwargs):
    with transaction(engine, write=True) as db:
        mature_undo_notifications(db)
    return worker.process_one(engine, *args, **kwargs)


def dispatch_batch_after_undo(db, *args, **kwargs):
    mature_undo_notifications(db)
    return worker.dispatch_batch(db, *args, **kwargs)
