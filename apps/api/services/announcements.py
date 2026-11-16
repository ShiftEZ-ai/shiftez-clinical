"""Announcement audiences, durable receipts and inbox delivery share one source record."""
from datetime import datetime, time, timedelta, timezone
import logging
from zoneinfo import ZoneInfo
from sqlalchemy import String, and_, cast, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.serialization import record
from packages.contracts import schemas as s
from packages.domain.time import aware
from . import audiences
from .common import expected, scoped, outbox
from .independence import today

log = logging.getLogger(__name__)


def active(row, day):
    return row.kind == "announcement" and row.active and row.starts_on <= day and (row.ends_on is None or day < row.ends_on)


def _insert(db, table, columns, query, unique):
    insert = postgres_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    return db.execute(insert(table).from_select(columns, query, include_defaults=False).on_conflict_do_nothing(index_elements=unique),
        execution_options={"preserve_rowcount": True})


def _id(db):
    # Generate an ID for each SELECT row, rather than evaluating the ORM's Python default once for the whole insert.
    return cast(func.gen_random_uuid(), String(36)) if db.bind.dialect.name == "postgresql" else func.lower(func.printf(
        "%s-%s-%s-%s-%s", func.hex(func.randomblob(4)), func.hex(func.randomblob(2)),
        func.hex(func.randomblob(2)), func.hex(func.randomblob(2)), func.hex(func.randomblob(6))))


def _recover_delivery(db, row):
    """Adopt pre-receipt deliveries without mistaking an old 'mark read' for explicit acknowledgement."""
    event = db.scalar(select(m.Outbox).where(m.Outbox.department_id == row.department_id,
        m.Outbox.kind == "announcement", m.Outbox.state == "available_in_app",
        m.Outbox.payload["content_id"].as_string() == row.id,
        select(m.Notification.id).where(m.Notification.outbox_id == m.Outbox.id).exists())
            .order_by(m.Outbox.created_at.desc(), m.Outbox.seq.desc()).limit(1))
    if event is None:
        return 0
    row.sent_outbox_id = event.id
    events = select(m.Outbox.id).where(m.Outbox.department_id == row.department_id,
        m.Outbox.kind == "announcement", m.Outbox.payload["content_id"].as_string() == row.id)
    note = m.Notification
    db.execute(update(note).where(note.outbox_id.in_(events), note.source_kind.is_(None))
        .values(source_kind="department_content", source_id=row.id))
    inserted = _insert(db, m.AnnouncementReceipt.__table__,
        ["id", "revision", "created_at", "content_id", "identity_id", "person_id", "delivered_at", "read_at"],
        select(_id(db), literal(1), func.min(note.created_at), literal(row.id), note.identity_id, m.Identity.person_id,
            func.min(note.created_at), func.min(note.read_at)).join(m.Identity, m.Identity.id == note.identity_id)
                .where(note.outbox_id.in_(events)).group_by(note.identity_id, m.Identity.person_id), ["content_id", "identity_id"])
    receipt = m.AnnouncementReceipt
    historical = select(func.min(note.read_at)).where(note.source_kind == "department_content", note.source_id == row.id,
        note.identity_id == receipt.identity_id).scalar_subquery()
    delivered = select(func.min(note.created_at)).where(note.source_kind == "department_content", note.source_id == row.id,
        note.identity_id == receipt.identity_id).scalar_subquery()
    db.execute(update(receipt).where(receipt.content_id == row.id,
        or_(and_(receipt.read_at.is_(None), historical.is_not(None)), and_(receipt.delivered_at.is_(None), delivered.is_not(None))))
        .values(read_at=func.coalesce(receipt.read_at, historical), delivered_at=func.coalesce(receipt.delivered_at, delivered),
            revision=receipt.revision + 1))
    return inserted.rowcount


def sync_receipts(db, row, day=None, *, recipient_context=None, deliver=True, identity_ids=None):
    """Return current target IDs and newly inserted receipt/notification count; departed receipts remain evidence."""
    day = day or today(db, row.department_id)
    if not active(row, day):
        return set(), 0
    inserted = 0
    if row.sent_outbox_id is None and row.receipts_checked_on is None:
        inserted += _recover_delivery(db, row)
    targets = audiences.recipients(db, row.department_id, row.audience, day, context=recipient_context)
    if identity_ids is not None:
        targets &= set(identity_ids)
    inserted += _insert(db, m.AnnouncementReceipt.__table__,
        ["id", "revision", "created_at", "content_id", "identity_id", "person_id"],
        select(_id(db), literal(1), literal(m.now()), literal(row.id), m.Identity.id, m.Identity.person_id)
            .where(m.Identity.id.in_(targets)), ["content_id", "identity_id"]).rowcount
    row.receipts_checked_on = day
    if deliver and row.sent_outbox_id:
        event = db.get(m.Outbox, row.sent_outbox_id)
        if event is not None and event.state == "available_in_app":
            inserted += _deliver(db, row, event, targets)
    return targets, inserted


def send(db, actor, row, *, notify=True):
    """A future announcement waits until the department's configured local send time."""
    if not notify or not row.active:
        return
    day = today(db, actor.department_id)
    department = db.get(m.Department, actor.department_id)
    zone = ZoneInfo(db.get(m.Organisation, actor.organisation_id).timezone)
    at = datetime.combine(row.starts_on, time.fromisoformat(department.announcement_send_time), zone).astimezone(timezone.utc) if row.starts_on > day else m.now()
    # The revision pins this send to what was saved: a later edit or switch-off makes it stale.
    outbox(db, actor, f"announcement:{row.id}:{row.revision}:{m.uid()}", "announcement",
           {"content_id": row.id, "revision": row.revision, "notify_inbox": notify}, at=at, undoable=True)


def _pending(row):
    receipt = m.AnnouncementReceipt
    return or_(and_(row.importance == "must_acknowledge", receipt.acknowledged_at.is_(None)),
               and_(row.importance != "must_acknowledge", receipt.read_at.is_(None)))


def notification_text(row):
    title = (("Important: " if row.importance in {"important", "must_acknowledge"} else "") + row.name)[:160]
    message = row.description or "Open Home for the details."
    return title, message if len(message) <= 500 else message[:480].rstrip() + "… (more on Home)"


def _deliver(db, row, event, targets):
    if not event.payload.get('notify_inbox', True):
        return 0
    from .notification_audience import filter_recipients
    targets = filter_recipients(db, event, targets)
    receipt = m.AnnouncementReceipt
    title, message = notification_text(row)
    query = select(_id(db), literal(1), literal(m.now()), literal(event.id), receipt.identity_id,
        literal(title), literal(message),
        literal(None) if event.payload.get("reminder") else receipt.read_at,
        receipt.acknowledged_at, literal("department_content"), literal(row.id)).where(
            receipt.content_id == row.id, receipt.identity_id.in_(targets))
    if event.payload.get("reminder"):
        query = query.where(_pending(row))
    inserted = _insert(db, m.Notification.__table__,
        ["id", "revision", "created_at", "outbox_id", "identity_id", "title", "message", "read_at", "acknowledged_at", "source_kind", "source_id"],
        query, ["outbox_id", "identity_id"])
    delivered = select(m.Notification.identity_id).where(m.Notification.outbox_id == event.id)
    db.execute(update(receipt).where(receipt.content_id == row.id, receipt.delivered_at.is_(None),
        receipt.identity_id.in_(delivered)).values(delivered_at=m.now(), revision=receipt.revision + 1))
    return inserted.rowcount


def deliver(db, event, *, recipient_context=None):
    """Bulk delivery, including the current audience and any reads already recorded on Home."""
    row = db.get(m.DepartmentContent, event.payload.get("content_id"))
    day = today(db, event.department_id)
    if row is None or not active(row, day) or row.revision != event.payload.get("revision"):
        return 0
    targets, _ = sync_receipts(db, row, day, recipient_context=recipient_context, deliver=False)
    count = _deliver(db, row, event, targets)
    if not event.payload.get("reminder") or row.sent_outbox_id is None:
        row.sent_outbox_id = event.id
    return count


def mark_read(db, actor, rows):
    """Mark rendered Home notices read only while they are current and still reach this actor."""
    day = today(db, actor.department_id)
    seen = audiences.viewer(db, actor, day)
    notices = [row for row in rows if row.department_id == actor.department_id and active(row, day)
               and audiences.reaches(db, actor, row.audience, day, seen)]
    if not notices:
        return {}
    ids, moment = [row.id for row in notices], m.now()
    receipt = m.AnnouncementReceipt
    # A new audience member can view Home before the next daily reconciliation.
    _insert(db, receipt.__table__, ["id", "revision", "created_at", "content_id", "identity_id", "person_id"],
        select(_id(db), literal(1), literal(moment), m.DepartmentContent.id, m.Identity.id, m.Identity.person_id)
            .select_from(m.DepartmentContent).join(m.Identity, m.Identity.id == actor.identity_id)
            .where(m.DepartmentContent.id.in_(ids)), ["content_id", "identity_id"])
    db.execute(update(receipt).where(receipt.content_id.in_(ids), receipt.identity_id == actor.identity_id,
        receipt.read_at.is_(None)).values(read_at=moment, revision=receipt.revision + 1))
    # Passive Home reading must not invalidate an already displayed inbox acknowledgement.
    db.execute(update(m.Notification).where(m.Notification.identity_id == actor.identity_id,
        m.Notification.source_kind == "department_content", m.Notification.source_id.in_(ids),
        m.Notification.read_at.is_(None)).values(read_at=moment))
    return {row.content_id: record(row) for row in db.scalars(select(receipt).where(
        receipt.content_id.in_(ids), receipt.identity_id == actor.identity_id))}


def counts(db, actor, rows):
    """Grouped, bounded queries count today's audience without loading individual receipt rows."""
    if not rows:
        return {}
    day = today(db, actor.department_id)
    context = audiences.recipient_context(db, actor.department_id, day)
    result = {row.id: {"audience": 0, "delivered": 0, "read": 0, "acknowledged": 0} for row in rows}
    groups = {}
    receipt = m.AnnouncementReceipt
    for row in rows:
        if row.kind != "announcement":
            continue
        targets = audiences.recipients(db, actor.department_id, row.audience, day, context=context)
        result[row.id]["audience"] = len(targets)
        groups.setdefault(frozenset(targets), set()).add(row.id)

    def collect(scopes):
        for content_id, delivered, read, acknowledged in db.execute(select(receipt.content_id,
            func.count(receipt.delivered_at), func.count(receipt.read_at), func.count(receipt.acknowledged_at))
                .where(or_(*scopes)).group_by(receipt.content_id)):
            for key, value in (("delivered", delivered), ("read", read), ("acknowledged", acknowledged)):
                result[content_id][key] += value

    scopes, parameters = [], 0
    for targets, ids in groups.items():
        targets, ids = list(targets), list(ids)
        # Bound SQL expressions and parameters even with years of distinct audiences; partitions never overlap.
        for target_start in range(0, len(targets), 15000):
            members = targets[target_start:target_start + 15000]
            maximum = 30000 - len(members)
            for start in range(0, len(ids), maximum):
                notices = ids[start:start + maximum]
                size = len(members) + len(notices)
                if scopes and (len(scopes) >= 100 or parameters + size > 30000):
                    collect(scopes)
                    scopes, parameters = [], 0
                scopes.append(and_(receipt.content_id.in_(notices), receipt.identity_id.in_(members)))
                parameters += size
    if scopes:
        collect(scopes)
    return result


def queue_reminder(db, row, *, actor_id=None, automatic=False, day=None, recipient_context=None, targets=None):
    """Manual and automatic reminders share the notice's department-configured cooldown."""
    day = day or today(db, row.department_id)
    require(active(row, day), "ANNOUNCEMENT_INACTIVE", "Only a current active announcement can be reminded")
    department, moment = db.get(m.Department, row.department_id), m.now()
    if row.last_reminded_at and aware(row.last_reminded_at) + timedelta(hours=department.announcement_reminder_interval_hours) > moment:
        require(automatic, "ANNOUNCEMENT_REMINDER_RATE_LIMIT", "Wait for the department's reminder interval before sending again", 429)
        return 0
    if targets is None:
        targets, _ = sync_receipts(db, row, day, recipient_context=recipient_context)
    pending = db.scalar(select(func.count()).select_from(m.AnnouncementReceipt).where(
        m.AnnouncementReceipt.content_id == row.id, m.AnnouncementReceipt.identity_id.in_(targets), _pending(row)))
    if not pending:
        if automatic:
            row.reminder_empty_checked_on = day
        return 0
    db.add(m.Outbox(department_id=row.department_id, event_key=f"announcement-reminder:{row.id}:{m.uid()}", kind="announcement",
        payload={"content_id": row.id, "revision": row.revision, "reminder": True, "actor_id": actor_id},
        next_attempt_at=moment))
    row.last_reminded_at = moment
    return pending


def remind(db, actor, command):
    require_role(actor, "admin", "coordinator")
    s.Decision.model_validate(command.payload)
    row = scoped(db, m.DepartmentContent, command.target_id, actor)
    expected(row, command.expected_revision)
    count = queue_reminder(db, row, actor_id=actor.identity_id)
    db.flush()
    return {"record": record(row), "reminded": count}


def reconcile(db, *, pending_only=False):
    """Recheck current notices once per local day, including Home-only notices and late joiners."""
    departments = list(db.scalars(select(m.Department).where(select(m.DepartmentContent.id).where(
        m.DepartmentContent.department_id == m.Department.id, m.DepartmentContent.kind == "announcement",
        m.DepartmentContent.active.is_(True)).exists())))
    changed = set()
    for department in departments:
        if pending_only:
            if _reconcile_department(db, department, pending_only=True):
                return True
            continue
        department_id = department.id
        try:
            with db.begin_nested():
                processed = _reconcile_department(db, department)
            if processed:
                changed.add(department_id)
        except Exception as exc:
            log.warning("announcement_reconcile_failed", extra={"department_id": department_id,
                                                               "error_code": type(exc).__name__})
    return changed


def _reconcile_department(db, department, *, pending_only=False):
    day, moment = today(db, department.id), m.now()
    unchecked = or_(m.DepartmentContent.receipts_checked_on.is_(None), m.DepartmentContent.receipts_checked_on < day)
    days = department.announcement_auto_reminder_days
    eligible = unchecked
    if days:
        cooldown = timedelta(hours=max(days * 24, department.announcement_reminder_interval_hours))
        pending = select(m.AnnouncementReceipt.id).where(
            m.AnnouncementReceipt.content_id == m.DepartmentContent.id, _pending(m.DepartmentContent)).exists()
        due = and_(m.DepartmentContent.starts_on <= day - timedelta(days=days),
            or_(m.DepartmentContent.reminder_empty_checked_on.is_(None), m.DepartmentContent.reminder_empty_checked_on < day),
            or_(m.DepartmentContent.last_reminded_at.is_(None), m.DepartmentContent.last_reminded_at <= moment - cooldown),
            pending)
        eligible = or_(unchecked, due)
    query = select(m.DepartmentContent).where(m.DepartmentContent.department_id == department.id,
        m.DepartmentContent.kind == "announcement", m.DepartmentContent.active.is_(True), m.DepartmentContent.starts_on <= day,
        or_(m.DepartmentContent.ends_on.is_(None), m.DepartmentContent.ends_on > day),
        eligible)
    if pending_only:
        return db.scalar(query.with_only_columns(m.DepartmentContent.id).limit(1)) is not None
    from apps.api.settings import Settings
    settings = db.get_bind().get_execution_options().get("rota_write_settings") or Settings()
    rows = list(db.scalars(query.order_by(m.DepartmentContent.id).limit(settings.worker_batch_size)))
    if not rows:
        return False
    context = audiences.recipient_context(db, department.id, day)
    changed = False
    for row in rows:
        if row.receipts_checked_on != day:
            targets, inserted = sync_receipts(db, row, day, recipient_context=context)
            changed = bool(inserted) or changed
        else:
            targets = audiences.recipients(db, department.id, row.audience, day, context=context)
        if days and day >= row.starts_on + timedelta(days=days) and (row.last_reminded_at is None or
                moment - aware(row.last_reminded_at) >= cooldown):
            reminded = queue_reminder(db, row, automatic=True, day=day, recipient_context=context, targets=targets)
            changed = bool(reminded) or changed
    return changed
