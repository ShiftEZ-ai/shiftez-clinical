"""Opening inbox items and explicitly acknowledging required messages are separate commands."""
from datetime import datetime

from sqlalchemy import case, func, select, update
from apps.api import models as m
from apps.api.errors import require
from apps.api.serialization import json_value, record
from packages.contracts import schemas as s
from . import audiences, event_delivery
from .common import expected


def _scope(db, actor):
    from .notification_audience import source_visibility
    content_id = m.Outbox.payload["content_id"].as_string()
    roles = getattr(actor, 'roles', None)
    generation = (m.Outbox.kind != 'generation_finished',) if roles is not None and 'coordinator' not in roles else ()
    # Legacy announcement envelopes without a source ID must still fail closed.
    return (m.Notification.identity_id == actor.identity_id, m.Outbox.department_id == actor.department_id,
        ((m.Outbox.kind != "announcement") & content_id.is_(None))
            | source_visibility(db, actor, m.DepartmentContent, content_id),
        (m.Outbox.kind != "event") | source_visibility(db, actor, m.Event, m.Outbox.payload["event_id"].as_string()),
        *generation)


def _kind():
    return case((m.Outbox.payload["request_id"].as_string().is_not(None), "request"), else_=m.Outbox.kind)


def _rows(db, actor, *filters, limit=100, offset=0):
    N, O = m.Notification, m.Outbox
    rows = db.execute(select(N, _kind(), O.payload, m.DepartmentContent.importance,
        m.EventOccurrence.data, m.EventOccurrence.cancelled, m.AnnouncementReceipt.acknowledged_at,
        m.SwapRequest.proposer_person_id, m.SwapRequest.recipient_person_id)
        .join(O, O.id == N.outbox_id)
        .outerjoin(m.SwapRequest, m.SwapRequest.id == O.payload["swap_id"].as_string())
        .outerjoin(m.EventOccurrence, (N.source_kind == "event") & (m.EventOccurrence.id == N.source_id))
        .outerjoin(m.DepartmentContent, (N.source_kind == "department_content")
            & (m.DepartmentContent.id == N.source_id) & (m.DepartmentContent.kind == "announcement"))
        .outerjoin(m.AnnouncementReceipt, ((m.AnnouncementReceipt.content_id == m.DepartmentContent.id)
            | (m.AnnouncementReceipt.event_occurrence_id == m.EventOccurrence.id))
            & (m.AnnouncementReceipt.identity_id == N.identity_id))
        .where(*_scope(db, actor), *filters).order_by(N.created_at.desc(), N.seq.desc()).limit(limit).offset(offset))
    links = ("workspace_id", "publication_id", "request_id", "swap_id", "feedback_report_id", "event_id", "staged_id")
    result = [{**record(row), "kind": kind,
        "acknowledged_at": json_value(acknowledged_at if row.source_kind in {"department_content", "event"}
                                      else row.acknowledged_at),
        "acknowledgement_required": importance == "must_acknowledge" or bool(
            data and not cancelled and data.get("type", {}).get("attendance") == "must_acknowledge"),
        'replace_action': (payload or {}).get('replace_action') if actor.permits('coordinator') else None,
        'duty_changes': ((payload or {}).get('person_changes') or {}).get(actor.person_id),
        'published_impact': {'count': payload['count'], 'sessions': payload.get('sessions', [])}
            if kind == 'credential_impact' and actor.permits('coordinator', 'approver', 'credentialer') else None,
        "link": {key: (payload or {}).get(key) for key in links if (payload or {}).get(key)
            and not (key == "swap_id" and (payload or {}).get("open_offer_step")
                and not ((payload or {}).get("open_offer_step") == "taken" and actor.person_id is not None
                         and actor.person_id in {proposer, recipient}))}}
        for row, kind, payload, importance, data, cancelled, acknowledged_at, proposer, recipient in rows]
    # Titles stored before SW3-C-07 named both swappers for every recipient; the swap row says who may keep them.
    from .common import SWAP_SUFFIX, may_name_swap, neutral_notification_title
    named = [item for item in result if item["kind"] == "publication" and SWAP_SUFFIX in (item["title"] or "")]
    ids = {item["link"].get("publication_id") for item in named} - {None}
    swappers = {pid: {a, b} for pid, a, b in db.execute(
        select(m.Publication.id, m.SwapRequest.proposer_person_id, m.SwapRequest.recipient_person_id)
        .join(m.SwapRequest, m.SwapRequest.workspace_id == m.Publication.workspace_id).where(m.Publication.id.in_(ids)))} if ids else {}
    for item in named:
        if not may_name_swap(actor.roles, actor.person_id, swappers.get(item["link"].get("publication_id"), set())):
            item["title"] = neutral_notification_title(item["title"])
    return result


def unread_snapshot(db, actor):
    count, revision = db.execute(select(func.count().filter(m.Notification.read_at.is_(None)),
        func.coalesce(func.sum(m.Notification.revision), 0) + func.count()).select_from(m.Notification).join(m.Outbox)
        .where(*_scope(db, actor))).one()
    return {'count': count, 'revision': revision, 'scope': 'department', 'department_id': actor.department_id}


def unread_total(db, actor):
    return unread_snapshot(db, actor)['count']


def list_all(db, actor, *, limit=100, offset=0, unread=False, kind=None, search="", search_options=None):
    filters = []
    if unread:
        filters.append(m.Notification.read_at.is_(None))
    if kind:
        filters.append(_kind() == kind)
    if search_options is not None:
        from .search import predicate
        filters.append(predicate(db, actor, 'notifications', search, search_options))
    elif search:
        filters.append(m.Notification.title.icontains(search, autoescape=True)
                       | m.Notification.message.icontains(search, autoescape=True))
    total, unread_count = db.execute(select(func.count().filter(*filters),
        func.count().filter(m.Notification.read_at.is_(None))).select_from(m.Notification)
        .join(m.Outbox).where(*_scope(db, actor))).one()
    return {"notifications": _rows(db, actor, *filters, limit=limit, offset=offset),
            "total": total, "unread_total": unread_count, "limit": limit, "offset": offset,
            "has_more": offset + limit < total}


def command(db, actor, value):
    if value.kind == "notification.unack":
        return unack(db, actor, s.NotificationUnack.model_validate(value.payload))
    schema = s.Decision if value.kind == "notification.acknowledge" else s.NotificationRead
    schema.model_validate(value.payload)
    N, R = m.Notification, m.AnnouncementReceipt
    receipt_sources = ("department_content", "event")
    owned = select(N.id).join(m.Outbox, m.Outbox.id == N.outbox_id).where(*_scope(db, actor))
    at = m.now()
    if value.kind == "notification.ack_all":
        routine = N.source_kind.is_(None) | N.source_kind.not_in(receipt_sources)
        previous = [dict(row) for row in db.execute(select(N.id, N.read_at, N.acknowledged_at, N.revision)
            .where(N.id.in_(owned), N.read_at.is_(None) | (routine & N.acknowledged_at.is_(None)))
            .order_by(N.id).limit(s.NOTIFICATION_UNDO_LIMIT + 1)).mappings()]
        require(len(previous) <= s.NOTIFICATION_UNDO_LIMIT, "NOTIFICATION_UNDO_LIMIT",
                f"Mark all as read supports up to {s.NOTIFICATION_UNDO_LIMIT:,} changed notifications at once", 409)
        sources = select(N.source_id).where(N.id.in_(owned), N.source_kind == "department_content")
        events = select(N.source_id).where(N.id.in_(owned), N.source_kind == "event")
        receipts = [dict(row) for row in db.execute(select(R.id, R.revision).where(
            R.identity_id == actor.identity_id, R.content_id.in_(sources) | R.event_occurrence_id.in_(events),
            R.read_at.is_(None)).limit(s.NOTIFICATION_UNDO_LIMIT + 1)).mappings()]
        require(len(receipts) <= s.NOTIFICATION_UNDO_LIMIT, "NOTIFICATION_UNDO_LIMIT",
                f"Mark all as read supports up to {s.NOTIFICATION_UNDO_LIMIT:,} changed receipts at once", 409)
        db.execute(update(R).where(R.id.in_([row["id"] for row in receipts]))
                   .values(read_at=at, revision=R.revision + 1))
        changed = db.execute(update(N).where(N.id.in_(owned),
            N.read_at.is_(None) | (routine & N.acknowledged_at.is_(None))).values(
                read_at=func.coalesce(N.read_at, at),
                acknowledged_at=case((routine, func.coalesce(N.acknowledged_at, at)), else_=N.acknowledged_at),
                revision=N.revision + 1)).rowcount
        # Keep the old reply key for existing callers; it now counts items marked read.
        ids = [row["id"] for row in previous]
        return {"acknowledged": changed, "read": changed, "notification_ids": ids,
                "previous_notifications": previous, "previous_receipts": receipts,
                "unread_total": unread_total(db, actor),
                "undo": {"kind": "notification.unack", "payload": {"notification_ids": ids,
                    "undo_of": db.info["receipt_id"], "reason": "Undo mark all as read"}} if ids or receipts else None}
    note = db.scalar(select(N).where(N.id.in_(owned), N.id == value.target_id))
    receipt = None
    acknowledging = value.kind == "notification.acknowledge"
    if note is None and value.kind == "notification.acknowledge":
        receipt = db.scalar(select(R).join(m.DepartmentContent, m.DepartmentContent.id == R.content_id).where(
            R.id == value.target_id, R.identity_id == actor.identity_id,
            m.DepartmentContent.department_id == actor.department_id))
    if note is None and receipt is None:
        receipt = db.scalar(select(R).join(m.EventOccurrence, m.EventOccurrence.id == R.event_occurrence_id).where(
            R.id == value.target_id, R.identity_id == actor.identity_id,
            m.EventOccurrence.department_id == actor.department_id))
    occurrence = None
    if note is None and receipt is None:
        occurrence = db.scalar(select(m.EventOccurrence).where(m.EventOccurrence.id == value.target_id,
            m.EventOccurrence.department_id == actor.department_id))
        require(occurrence is not None and audiences.reaches(db, actor, occurrence.data.get("audience"),
                event_delivery.local_day(db, occurrence)), "NOT_FOUND", "Notification not found", 404)
    expected(note if note is not None else receipt if receipt is not None else occurrence, value.expected_revision)
    if note is not None and note.source_kind == "department_content":
        receipt = db.scalar(select(R).where(R.content_id == note.source_id, R.identity_id == actor.identity_id))
    elif note is not None and note.source_kind == "event":
        receipt = db.scalar(select(R).where(R.event_occurrence_id == note.source_id, R.identity_id == actor.identity_id))
    occurrence_id = receipt.event_occurrence_id if receipt is not None else (
        note.source_id if note is not None and note.source_kind == "event" else None)
    if occurrence is not None:
        occurrence_id = occurrence.id
    if occurrence_id:
        if occurrence is None:
            occurrence = db.get(m.EventOccurrence, occurrence_id)
            if acknowledging or note is None or receipt is None:
                require(occurrence is not None and occurrence.department_id == actor.department_id
                        and audiences.reaches(db, actor, occurrence.data.get("audience"), event_delivery.local_day(db, occurrence)),
                        "NOT_FOUND", "Notification not found", 404)
        if acknowledging:
            require(not occurrence.cancelled and occurrence.data.get("type", {}).get("attendance") == "must_acknowledge",
                    "ACKNOWLEDGEMENT_NOT_REQUIRED", "This notification does not require acknowledgement", 409)
        if receipt is None:
            event_delivery.sync_receipts(db, occurrence, identity_id=actor.identity_id)
            receipt = db.scalar(select(R).where(R.event_occurrence_id == occurrence_id, R.identity_id == actor.identity_id))
            require(receipt is not None, "NOT_FOUND", "Notification not found", 404)
    elif acknowledging:
        content_id = receipt.content_id if receipt is not None else (note.source_id if note is not None else None)
        content = db.get(m.DepartmentContent, content_id) if content_id else None
        required = content is not None and content.department_id == actor.department_id and content.importance == "must_acknowledge"
        require(required, "ACKNOWLEDGEMENT_NOT_REQUIRED", "This notification does not require acknowledgement", 409)
    for row in (note, receipt):
        if row is None:
            continue
        # Routine reads also populate legacy acknowledged_at; announcement/event acknowledgement stays explicit.
        acknowledge_row = acknowledging or (row is note and note.source_kind not in receipt_sources)
        changed = row.read_at is None or (acknowledge_row and row.acknowledged_at is None)
        row.read_at = row.read_at or at
        if acknowledge_row:
            row.acknowledged_at = row.acknowledged_at or at
        if changed:
            row.revision += 1
    if receipt is not None:
        # All reminders point to the same receipt; reading/acknowledging one updates its siblings in a set.
        values = {"read_at": func.coalesce(N.read_at, receipt.read_at), "revision": N.revision + 1}
        pending = N.read_at.is_(None)
        if acknowledging:
            values["acknowledged_at"] = func.coalesce(N.acknowledged_at, receipt.acknowledged_at)
            pending = pending | N.acknowledged_at.is_(None)
        source_kind = "event" if receipt.event_occurrence_id else "department_content"
        source_id = receipt.event_occurrence_id or receipt.content_id
        db.execute(update(N).where(N.identity_id == actor.identity_id, N.source_kind == source_kind,
            N.source_id == source_id, pending).values(**values))
    db.flush()
    return {**({"notification": record(note)} if note is not None else {}),
            **({"receipt": record(receipt)} if receipt is not None else {}),
            "notifications": _rows(db, actor, N.id == note.id) if note is not None else [],
            "unread_total": unread_total(db, actor)}


def unack(db, actor, value):
    source = db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.id == value.undo_of,
        m.CommandReceipt.department_id == actor.department_id, m.CommandReceipt.actor_id == actor.identity_id,
        m.CommandReceipt.command_kind == "notification.ack_all"))
    require(source is not None, "NOT_FOUND", "Mark-all-read command not found", 404)
    previous = source.result.get("previous_notifications", [])
    require(set(value.notification_ids) == {row["id"] for row in previous}, "NOTIFICATION_UNDO_MISMATCH",
            "Undo must restore the notifications changed by this command", 409)
    # A fresh idempotency key must not reapply an old Undo after the inbox was read again.
    undone = db.scalar(select(m.CommandReceipt.id).where(m.CommandReceipt.department_id == actor.department_id,
        m.CommandReceipt.actor_id == actor.identity_id, m.CommandReceipt.command_kind == "notification.unack",
        m.CommandReceipt.result["undo_of"].as_string() == source.id).limit(1))
    if undone:
        return {"undo_of": source.id, "notification_ids": [], "unread": 0, "unread_total": unread_total(db, actor)}
    N, R = m.Notification, m.AnnouncementReceipt
    notes = {row.id: row for row in db.scalars(select(N).join(m.Outbox).where(
        *_scope(db, actor), N.id.in_(value.notification_ids)))}
    receipt_before = source.result.get("previous_receipts", [])
    receipts = {row.id: row for row in db.scalars(select(R).where(R.identity_id == actor.identity_id,
        R.id.in_([item["id"] for item in receipt_before])))}
    require(len(notes) == len(previous) and len(receipts) == len(receipt_before),
            "NOT_FOUND", "Notification not found", 404)
    for before, rows in ((previous, notes), (receipt_before, receipts)):
        for item in before:
            expected(rows[item["id"]], item["revision"] + 1)
    for item in previous:
        note = notes[item["id"]]
        note.read_at = datetime.fromisoformat(item["read_at"]) if item["read_at"] else None
        note.acknowledged_at = datetime.fromisoformat(item["acknowledged_at"]) if item["acknowledged_at"] else None
        note.revision += 1
    for receipt in receipts.values():
        receipt.read_at = None
        receipt.revision += 1
    db.flush()
    return {"undo_of": source.id, "notification_ids": value.notification_ids,
            "unread": sum(item["read_at"] is None for item in previous), "unread_total": unread_total(db, actor)}
