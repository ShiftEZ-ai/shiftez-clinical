"""Event revisions use the existing outbox and durable recipient receipts."""
from zoneinfo import ZoneInfo

from sqlalchemy import case, literal, select, update

from apps.api import models as m
from packages.domain.time import aware
from . import audiences
from .announcements import _id, _insert
from .common import outbox


def local_day(db, occurrence):
    department = db.get(m.Department, occurrence.department_id)
    return aware(occurrence.start).astimezone(ZoneInfo(db.get(m.Organisation, department.organisation_id).timezone)).date()


def queue(db, actor, event, occurrence_ids=None, *, key_suffix=''):
    payload = {"event_id": event.id, "revision": event.revision}
    if occurrence_ids is not None:
        changed = set(occurrence_ids)
        pending = db.scalars(select(m.Outbox.payload).where(m.Outbox.department_id == event.department_id,
            m.Outbox.kind == "event", m.Outbox.state.in_(["queued", "retry"]),
            m.Outbox.payload["event_id"].as_string() == event.id))
        # A newer revision supersedes earlier sends, so carry forward their still-undelivered occurrence changes.
        for previous in pending:
            if "occurrence_ids" not in previous:
                break
            changed.update(previous["occurrence_ids"])
        else:
            payload["occurrence_ids"] = sorted(changed)
    outbox(db, actor, f"event:{event.id}:{event.revision}{key_suffix}", "event", payload, undoable=True)


def targets(db, occurrence, context=None, *, current_only=False, previous=None):
    """Cancellations also reach prior recipients, including people who gained an account after email delivery."""
    day = context[2][None]['date'] if context is not None else local_day(db, occurrence)
    context = context if context is not None else audiences.recipient_context(db, occurrence.department_id, day)
    targets = audiences.recipients(db, occurrence.department_id, occurrence.data.get("audience"),
                                  day, context=context)
    if occurrence.cancelled and not current_only:
        if previous is None:
            previous = (set(db.scalars(select(m.AnnouncementReceipt.identity_id).where(
                m.AnnouncementReceipt.event_occurrence_id == occurrence.id))),
                set(db.scalars(select(m.EmailDelivery.person_id).where(
                    m.EmailDelivery.event_occurrence_id == occurrence.id, m.EmailDelivery.person_id.is_not(None)))))
        targets |= {identity.id for identity in context[1] if identity.id in previous[0] or identity.person_id in previous[1]}
    return targets


def sync_receipts(db, occurrence, context=None, *, identity_id=None, identity_ids=None):
    """Insert requested audience receipts; return all targets and preserve existing recipient evidence."""
    recipients = targets(db, occurrence, context)
    selected = recipients if identity_ids is None else recipients & set(identity_ids)
    if identity_id is not None:
        selected = selected & {identity_id}
    _insert(db, m.AnnouncementReceipt.__table__,
        ["id", "revision", "created_at", "event_occurrence_id", "identity_id", "person_id"],
        select(_id(db), literal(1), literal(m.now()), literal(occurrence.id), m.Identity.id, m.Identity.person_id)
            .where(m.Identity.id.in_(selected)), ["event_occurrence_id", "identity_id"])
    return recipients


def deliver(db, event, *, recipient_context=None):
    """Skip superseded revisions; one revision notification can refer to the first affected occurrence."""
    row = db.get(m.Event, event.payload.get("event_id"))
    if row is None or row.department_id != event.department_id or row.revision != event.payload.get("revision"):
        return 0
    query = select(m.EventOccurrence).where(m.EventOccurrence.event_id == row.id,
                                            m.EventOccurrence.department_id == event.department_id)
    if "occurrence_ids" in event.payload:
        query = query.where(m.EventOccurrence.id.in_(event.payload["occurrence_ids"]))
    occurrences = list(db.scalars(query.order_by(m.EventOccurrence.start, m.EventOccurrence.id)))
    contexts = recipient_context if recipient_context is not None else db.info.setdefault("event_recipient_contexts", {})
    already = set(db.scalars(select(m.Notification.identity_id).where(m.Notification.outbox_id == event.id)))
    receipt, inserted = m.AnnouncementReceipt, 0
    for occurrence in occurrences:
        if not occurrence.data.get("type", {}).get("notify_inbox", True):
            continue
        day = local_day(db, occurrence)
        key = (event.department_id, day)
        if key not in contexts:
            contexts[key] = audiences.recipient_context(db, *key)
        context = contexts[key]
        raw_recipients = targets(db, occurrence, context)
        from .notification_audience import filter_recipients
        recipients = filter_recipients(db, event, raw_recipients)
        sync_receipts(db, occurrence, context, identity_ids=recipients)
        current_targets = targets(db, occurrence, context, current_only=True) & recipients
        title = (("Cancelled: " if occurrence.cancelled else "") + occurrence.data.get("heading", ""))[:160]
        message = occurrence.data.get("description") or "Open Requests for the details."
        pending = recipients - already
        in_audience = receipt.identity_id.in_(current_targets)
        result = _insert(db, m.Notification.__table__,
            ["id", "revision", "created_at", "outbox_id", "identity_id", "title", "message", "read_at",
             "acknowledged_at", "source_kind", "source_id"],
            select(_id(db), literal(1), literal(m.now()), literal(event.id), receipt.identity_id,
                   case((in_audience, title), else_="Cancelled: Event"),
                   case((in_audience, message[:500]), else_="An event previously shared with you has been cancelled."),
                   receipt.read_at, receipt.acknowledged_at,
                   literal("event"), literal(occurrence.id)).where(
                       receipt.event_occurrence_id == occurrence.id, receipt.identity_id.in_(pending)),
            ["outbox_id", "identity_id"])
        inserted += result.rowcount
        already |= pending
        # The revision notification covers this event's occurrences, even though its link opens the first one.
        delivered = select(m.Notification.identity_id).where(m.Notification.outbox_id == event.id)
        db.execute(update(receipt).where(receipt.event_occurrence_id == occurrence.id,
            receipt.identity_id.in_(recipients), receipt.identity_id.in_(delivered), receipt.delivered_at.is_(None))
            .values(delivered_at=m.now(), revision=receipt.revision + 1))
    return inserted
