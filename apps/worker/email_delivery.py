"""Recipient email outbox; provider acceptance never records a read or acknowledgement."""
import base64
from datetime import timedelta
from email import policy
from email.utils import parsedate_to_datetime
import hashlib
import logging
import os
import ipaddress
import smtplib
import socket
import ssl
from types import SimpleNamespace

import httpx
from sqlalchemy import select, text

from apps.api import models as m
from apps.api.auth import current_roles, department_roles
from apps.api.errors import Problem
from apps.api.services import pwa, announcements, audiences, email_audiences, email_messages, event_delivery, mailboxes
from apps.api.services.independence import today
from apps.api.settings import secret_from_env
from packages.contracts import schemas as s
from packages.domain.time import aware

log = logging.getLogger("rota.worker.email")


class DeliveryError(Exception):
    def __init__(self, code, *, retry=False, retry_after=0, draft=False):
        super().__init__(code)
        self.code, self.retry, self.retry_after, self.draft = code, retry, retry_after, draft


def _source(db, parent, *, occurrence_id=None, occurrence_revision=None):
    if parent.kind == "announcement":
        if not parent.payload.get("notify_inbox", True):
            return None
        row = db.get(m.DepartmentContent, parent.payload.get("content_id"))
        if row is None or not announcements.active(row, today(db, parent.department_id)):
            return None
    elif parent.kind == "event":
        row = db.get(m.Event, parent.payload.get("event_id"))
        if occurrence_id is not None:
            occurrence = db.get(m.EventOccurrence, occurrence_id)
            if (row is None or row.department_id != parent.department_id or occurrence is None
                    or occurrence.department_id != parent.department_id or occurrence.event_id != row.id
                    or occurrence.revision != occurrence_revision):
                return None
            return row
    else:
        return None
    return row if row is not None and row.department_id == parent.department_id and row.revision == parent.payload.get("revision") else None


def _choice(row, config):
    data = row.data if isinstance(row, m.EventOccurrence) else vars(row)
    enabled = data.get("also_email")
    if enabled is None:
        enabled = data.get("type", {}).get("also_email", False) if isinstance(row, m.EventOccurrence) else config.announcement_default
    return (data.get("email_sender") or config.default_sender) if enabled else None


def _enabled(config, mode):
    return (mode == "smtp" and config.smtp_enabled) or (mode == "microsoft" and config.m365_enabled)


def _pending(row, receipt, parent):
    if not parent.payload.get("reminder"):
        return True
    return receipt.acknowledged_at is None if row.importance == "must_acknowledge" else receipt.read_at is None


def _queue_recipient(db, parent, source, mode, sender_id, identity, address, *, receipt=None):
    occurrence_id = source.id if isinstance(source, m.EventOccurrence) else None
    suffix = receipt.id if receipt else f"person:{identity.person_id}:{occurrence_id or ''}"
    child = m.Outbox(department_id=parent.department_id, event_key=f"email:{parent.id}:{suffix}",
                     kind="email", payload={"source_outbox_id": parent.id,
                         **({"occurrence_revision": source.revision} if occurrence_id else {})})
    db.add(child)
    db.flush()
    db.add(m.EmailDelivery(department_id=parent.department_id, outbox_id=child.id,
        source_outbox_id=parent.id, receipt_id=receipt.id if receipt else None, identity_id=identity.id,
        person_id=identity.person_id, event_occurrence_id=occurrence_id,
        sender_identity_id=sender_id, sender_mode=mode, address=address,
        state="queued" if address else "failed", error_code=None if address else "EMAIL_ADDRESS_MISSING"))
    if not address:
        child.state, child.last_error = "failed", "EMAIL_ADDRESS_MISSING"


def queue(db, parent):
    """Queue each current recipient once, including events whose inbox channel is off."""
    row = _source(db, parent)
    if row is None:
        return queue_notifications(db, parent)
    config = s.EmailSettings.model_validate(db.get(m.Department, parent.department_id).email_settings)
    if not (config.smtp_enabled or config.m365_enabled):
        return 0
    if parent.kind == "announcement":
        sources = [row]
    else:
        query = select(m.EventOccurrence).where(m.EventOccurrence.event_id == row.id,
                                                m.EventOccurrence.department_id == parent.department_id)
        if "occurrence_ids" in parent.payload:
            query = query.where(m.EventOccurrence.id.in_(parent.payload["occurrence_ids"]))
        sources = list(db.scalars(query.order_by(m.EventOccurrence.start, m.EventOccurrence.id)))
    existing = list(db.scalars(select(m.EmailDelivery).where(m.EmailDelivery.source_outbox_id == parent.id)))
    done = {delivery.receipt_id for delivery in existing if delivery.receipt_id}
    done_people = {(delivery.person_id, delivery.event_occurrence_id) for delivery in existing if delivery.identity_id is None}
    count = 0
    policy = pwa.policy(db, parent.department_id)[0]
    contacts_prefs = None
    for source in sources:
        mode = _choice(source, config)
        is_event = isinstance(source, m.EventOccurrence)
        day = event_delivery.local_day(db, source) if is_event else today(db, parent.department_id)
        audience = source.data.get("audience") if is_event else source.audience
        target_ids = (event_delivery.targets(db, source) if is_event else
                      audiences.recipients(db, parent.department_id, audience, day))
        from apps.api.services.notification_audience import filter_people, filter_recipients
        target_ids = filter_recipients(db, parent, target_ids)
        contact_rows = list(db.execute(select(m.Identity, m.Person, m.Account).outerjoin(
            m.Person, m.Person.id == m.Identity.person_id).outerjoin(
            m.Account, m.Account.identity_id == m.Identity.id).where(m.Identity.id.in_(target_ids)))) if target_ids else []
        contacts_prefs = pwa.preference_rows(db, parent.department_id, target_ids)
        eligible = set()
        for identity, person, account in contact_rows:
            pref = contacts_prefs.get(identity.id)
            pref_value = s.NotificationPreferences.model_validate(pref.definition if pref else {}).model_dump(mode='json')
            choice = pwa.channel_setting(db, parent.department_id, identity.id, parent.kind, 'email', policy_value=policy,
                                         pref_value=pref_value)
            recipient_mode = mode or config.default_sender if choice == 'on' else mode
            if choice != 'off' and _enabled(config, recipient_mode):
                eligible.add(identity.id)
        if is_event:
            targets = event_delivery.sync_receipts(db, source, identity_ids=eligible) if eligible else set()
            query = select(m.AnnouncementReceipt).where(m.AnnouncementReceipt.event_occurrence_id == source.id)
        else:
            targets, _ = announcements.sync_receipts(db, source, deliver=False, identity_ids=eligible) if eligible else (set(), 0)
            query = select(m.AnnouncementReceipt).where(m.AnnouncementReceipt.content_id == source.id)
        sender_id = email_messages.sender_identity_id(db, source, parent)
        contacts = query.add_columns(m.Identity, m.Person, m.Account).join(
            m.Identity, m.Identity.id == m.AnnouncementReceipt.identity_id).outerjoin(
            m.Person, m.Person.id == m.Identity.person_id).outerjoin(
            m.Account, m.Account.identity_id == m.Identity.id)
        contact_rows = list(db.execute(contacts.where(m.AnnouncementReceipt.identity_id.in_(eligible)))) if eligible else []
        contacts_prefs = pwa.preference_rows(db, parent.department_id, eligible)
        for receipt, identity, person, account in contact_rows:
            pref = contacts_prefs.get(identity.id)
            pref_value = s.NotificationPreferences.model_validate(pref.definition if pref else {}).model_dump(mode='json')
            choice = pwa.channel_setting(db, parent.department_id, identity.id, parent.kind, 'email', policy_value=policy, pref_value=pref_value)
            recipient_mode = mode or config.default_sender if choice == 'on' else mode
            if choice == 'off' or not _enabled(config, recipient_mode):
                continue
            if receipt.id in done or not _pending(row, receipt, parent):
                continue
            _queue_recipient(db, parent, source, recipient_mode, sender_id, identity,
                email_messages._address(identity, person, account), receipt=receipt)
            done.add(receipt.id)
            count += 1
        people = {person.id: person for person in email_audiences.extra_people(db, parent.department_id, audience, day)}
        if is_event and source.cancelled:
            previous = list(db.scalars(select(m.EmailDelivery.person_id).where(
                m.EmailDelivery.event_occurrence_id == source.id, m.EmailDelivery.identity_id.is_(None))))
            people.update({person.id: person for person in email_audiences.extra_people(db, parent.department_id,
                {"person_ids": previous}, day)})
        for person in filter_people(db, parent, people.values()):
            if not _enabled(config, mode):
                continue
            key = (person.id, source.id if is_event else None)
            if key not in done_people:
                identity = SimpleNamespace(id=None, person_id=person.id, disabled=False)
                _queue_recipient(db, parent, source, mode, sender_id, identity,
                    email_messages._address(identity, person, None))
                done_people.add(key)
                count += 1
    db.flush()
    return count


def queue_notifications(db, parent):
    if parent.kind in ('announcement', 'event', 'email', 'push'):
        return 0
    config = s.EmailSettings.model_validate(db.get(m.Department, parent.department_id).email_settings)
    if not _enabled(config, config.default_sender):
        return 0
    contacts = list(db.execute(select(m.Notification, m.Identity, m.Person, m.Account).join(
        m.Identity, m.Identity.id == m.Notification.identity_id).outerjoin(m.Person, m.Person.id == m.Identity.person_id)
        .outerjoin(m.Account, m.Account.identity_id == m.Identity.id).where(m.Notification.outbox_id == parent.id)))
    prefs = pwa.preference_rows(db, parent.department_id, [identity.id for _, identity, _, _ in contacts])
    policy = pwa.policy(db, parent.department_id)[0]
    roles = department_roles(db, parent.department_id)
    done = set(db.scalars(select(m.EmailDelivery.identity_id).where(m.EmailDelivery.source_outbox_id == parent.id)))
    count = 0
    for note, identity, person, account in contacts:
        event_id = pwa.classify_event(parent, identity, roles.get(identity.id, ()))
        pref = prefs.get(identity.id)
        pref_value = s.NotificationPreferences.model_validate(pref.definition if pref else {}).model_dump(mode='json')
        if identity.disabled or identity.id in done or not pwa.channel_allowed(db, parent.department_id, identity.id,
                event_id, 'email', policy_value=policy, pref_value=pref_value):
            continue
        child = m.Outbox(department_id=parent.department_id, kind='email', event_key=f'notification-email:{note.id}',
                         payload={'notification_id': note.id, 'event_id': event_id})
        db.add(child); db.flush()
        db.add(m.EmailDelivery(department_id=parent.department_id, outbox_id=child.id, source_outbox_id=parent.id,
            identity_id=identity.id, person_id=identity.person_id, sender_identity_id=parent.payload.get('actor_id'),
            sender_mode=config.default_sender, address=email_messages._address(identity, person, account), state='queued'))
        count += 1
    return count


def _eligible_notification(db, delivery, parent, child):
    identity = db.get(m.Identity, delivery.identity_id)
    if parent is None or parent.state not in ('available_in_app', 'queued', 'retry') or not identity or identity.disabled:
        raise DeliveryError('EMAIL_ACCESS_ENDED')
    note = db.get(m.Notification, child.payload['notification_id'])
    if not pwa.notification_reaches(db, note) or not pwa.channel_allowed(db, parent.department_id,
            identity.id, child.payload['event_id'], 'email'):
        raise DeliveryError('EMAIL_ACCESS_ENDED')
    return identity, None


def _notification_message(db, child, identity, config, settings):
    from email.utils import formataddr
    from packages.domain.email import build_message
    title = pwa.CATALOGUE[child.payload['event_id']]['label']
    generic = pwa.policy(db, child.department_id)[0]['generic_message']
    return build_message(heading=title, details=generic,
        acknowledge_url=settings.public_origin + '/n/' + child.payload['notification_id'],
        recipients=[email_messages.address_for(db, identity)],
        sender=formataddr((config.sender_name, config.sender_address)) if config.default_sender == 'smtp' else None,
        draft=False)


class RestrictedSMTP(smtplib.SMTP):
    """Resolve once and connect only to validated addresses, retaining the host for STARTTLS."""
    def __init__(self, host, port, *, timeout, allowed_hosts):
        self.allowed_hosts = {value.strip().rstrip(".").casefold() for value in allowed_hosts.split(",") if value.strip()}
        super().__init__(host, port, timeout=timeout)

    def _get_socket(self, host, port, timeout):
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses:
            raise OSError("SMTP host has no addresses")
        if host.rstrip(".").casefold() not in self.allowed_hosts:
            for _, _, _, _, address in addresses:
                ip = ipaddress.ip_address(address[0].split("%", 1)[0])
                ip = getattr(ip, "ipv4_mapped", None) or ip
                if not ip.is_global or ip.is_multicast:
                    raise DeliveryError("SMTP_UNCONFIGURED", draft=True)
        last = None
        for family, kind, protocol, _, address in addresses:
            sock = socket.socket(family, kind, protocol)
            try:
                sock.settimeout(timeout)
                sock.connect(address)
                return sock
            except OSError as exc:
                sock.close()
                last = exc
        raise last


def send_smtp(db, delivery, message, config, settings):
    if not config.sender_address:
        raise DeliveryError("SMTP_UNCONFIGURED", draft=True)
    try:
        password = secret_from_env("FMUC_SMTP_PASSWORD") if config.smtp_username else ""
        if config.smtp_username and not password:
            raise DeliveryError("SMTP_UNCONFIGURED", draft=True)
        # Deployment credentials must never follow an administrator's relay destination.
        host = os.getenv("FMUC_SMTP_HOST", "") if password else config.smtp_host
        port = int(os.getenv("FMUC_SMTP_PORT", "587")) if password else config.smtp_port
        if not host or any(c.isspace() or c in '/\\@:' or ord(c) < 32 or ord(c) == 127 for c in host) or not 1 <= port <= 65535:
            raise DeliveryError("SMTP_UNCONFIGURED", draft=True)
        with RestrictedSMTP(host, port, timeout=config.timeout_seconds, allowed_hosts=settings.smtp_private_host_allowlist) as client:
            if password or config.smtp_starttls:
                client.starttls(context=ssl.create_default_context())
            if config.smtp_username:
                client.login(config.smtp_username, password)
            # send_message removes Bcc from the wire while preserving its envelope recipient.
            refused = client.send_message(message, from_addr=config.sender_address, to_addrs=[delivery.address])
            if refused:
                raise DeliveryError("SMTP_RECIPIENT_REJECTED", retry=any(400 <= value[0] < 500 for value in refused.values()))
    except smtplib.SMTPAuthenticationError:
        raise DeliveryError("SMTP_AUTH_FAILED", draft=True) from None
    except ssl.SSLError:
        raise DeliveryError("SMTP_TLS_FAILED", draft=True) from None
    except (RuntimeError, ValueError, smtplib.SMTPNotSupportedError):
        raise DeliveryError("SMTP_UNCONFIGURED", draft=True) from None
    except smtplib.SMTPRecipientsRefused as exc:
        raise DeliveryError("SMTP_RECIPIENT_REJECTED", retry=any(400 <= value[0] < 500 for value in exc.recipients.values())) from None
    except smtplib.SMTPResponseException as exc:
        raise DeliveryError("SMTP_REJECTED", retry=400 <= exc.smtp_code < 500) from None
    except (OSError, smtplib.SMTPException):
        raise DeliveryError("SMTP_UNAVAILABLE", retry=True) from None


def retry_after(value):
    """Accept Retry-After delay seconds or an HTTP date without shortening the provider delay."""
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        try:
            return max(0, (aware(parsedate_to_datetime(value)) - m.now()).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


def send_microsoft(db, delivery, message, config, settings):
    try:
        token = mailboxes.access_token(db, delivery.sender_identity_id, delivery.department_id, settings)
    except Exception:
        raise DeliveryError("MAILBOX_UNAVAILABLE", draft=True) from None
    try:
        # MIME preserves the shared calendar attachment; sendMail saves to the signed-in mailbox's Sent Items.
        with httpx.Client(timeout=config.timeout_seconds, follow_redirects=False, trust_env=False) as client:
            response = client.post(config.m365_graph_url.rstrip("/") + "/me/sendMail",
                headers={"Authorization": "Bearer " + token, "Content-Type": "text/plain"},
                content=base64.b64encode(message.as_bytes(policy=policy.SMTP)))
    except httpx.HTTPError:
        raise DeliveryError("GRAPH_UNAVAILABLE", retry=True) from None
    if response.status_code == 202:
        return
    if response.status_code in {401, 403}:
        raise DeliveryError("MAILBOX_UNAVAILABLE", draft=True)
    if response.status_code == 429:
        raise DeliveryError("GRAPH_THROTTLED", retry=True, retry_after=retry_after(response.headers.get("Retry-After")))
    raise DeliveryError("GRAPH_UNAVAILABLE" if response.status_code >= 500 else "GRAPH_REJECTED",
                        retry=response.status_code >= 500)


def send(db, delivery, message, config, settings):
    if delivery.sender_mode == "smtp":
        return send_smtp(db, delivery, message, config, settings)
    if delivery.sender_mode == "microsoft":
        return send_microsoft(db, delivery, message, config, settings)
    raise DeliveryError("EMAIL_SENDER_UNAVAILABLE", draft=True)


def _eligible(db, delivery, parent, *, contexts, identity_ids):
    pin = {}
    if parent is not None and parent.kind == "event":
        child = db.get(m.Outbox, delivery.outbox_id)
        if (child is None or child.department_id != parent.department_id or delivery.department_id != parent.department_id
                or child.payload.get("source_outbox_id") != parent.id or delivery.event_occurrence_id is None):
            raise DeliveryError("STALE_EMAIL_SOURCE")
        if "occurrence_revision" in child.payload:
            pin = {"occurrence_id": delivery.event_occurrence_id,
                   "occurrence_revision": child.payload["occurrence_revision"]}
    row = _source(db, parent, **pin) if parent is not None else None
    if row is None:
        raise DeliveryError("STALE_EMAIL_SOURCE")
    from apps.api.services.notification_audience import filter_people, filter_recipients
    if delivery.identity_id is None:
        person = db.get(m.Person, delivery.person_id)
        if person is None or not filter_people(db, parent, [person], audit=False):
            raise DeliveryError("EMAIL_ACCESS_ENDED")
        source = db.get(m.EventOccurrence, delivery.event_occurrence_id) if parent.kind == "event" else row
        if source is None or parent.kind == "event" and source.event_id != row.id:
            raise DeliveryError("STALE_EMAIL_SOURCE")
        audience = source.data.get("audience") if parent.kind == "event" else row.audience
        day = event_delivery.local_day(db, source) if parent.kind == "event" else today(db, parent.department_id)
        if parent.kind == "event" and source.cancelled:
            audience = {"person_ids": [delivery.person_id]}
        if not any(person.id == delivery.person_id for person in email_audiences.extra_people(db, parent.department_id, audience, day)):
            raise DeliveryError("EMAIL_ACCESS_ENDED")
        return SimpleNamespace(id=None, person_id=delivery.person_id, disabled=False), None
    identity = db.get(m.Identity, delivery.identity_id)
    if identity is None or identity.disabled or not current_roles(db, identity.id, parent.department_id):
        raise DeliveryError("EMAIL_ACCESS_ENDED")
    if not filter_recipients(db, parent, {identity.id}, {identity.id: identity}, audit=False):
        raise DeliveryError("EMAIL_ACCESS_ENDED")
    receipt = db.get(m.AnnouncementReceipt, delivery.receipt_id)
    if receipt is None or not _pending(row, receipt, parent):
        raise DeliveryError("EMAIL_NO_LONGER_PENDING")
    if parent.kind == "event":
        occurrence = db.get(m.EventOccurrence, receipt.event_occurrence_id)
        if (occurrence is None or occurrence.event_id != row.id or receipt.identity_id != delivery.identity_id
                or occurrence.id != delivery.event_occurrence_id):
            raise DeliveryError("STALE_EMAIL_SOURCE")
        if occurrence.cancelled:
            return identity, receipt
        audience, day = occurrence.data.get("audience"), event_delivery.local_day(db, occurrence)
    else:
        audience, day = row.audience, today(db, parent.department_id)
    key = (parent.department_id, day)
    if key not in contexts:
        contexts[key] = audiences.recipient_context(db, *key, identity_ids=identity_ids)
    active, _, holds, tags = contexts[key]
    if identity.id not in active or not audiences.matches(audience, active[identity.id], identity.person_id,
            holds.get(identity.person_id, holds[None]), tags):
        raise DeliveryError("EMAIL_ACCESS_ENDED")
    return identity, receipt


def _sender_available(db, delivery):
    if db.bind.dialect.name != "postgresql":
        return True
    sender = delivery.sender_identity_id if delivery.sender_mode == "microsoft" else ""
    key = int.from_bytes(hashlib.blake2b(f"email:{delivery.department_id}:{delivery.sender_mode}:{sender}".encode(),
                                       digest_size=8).digest(), signed=True)
    return db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})


def dispatch(db, settings, limit=200, department_id=None):
    """Lock due children so concurrent workers cannot send the same pending recipient."""
    query = select(m.Outbox).where(m.Outbox.kind == "email", m.Outbox.state.in_(["queued", "retry"]),
        m.Outbox.next_attempt_at <= m.now())
    if department_id is not None:
        query = query.where(m.Outbox.department_id == department_id)
    query = query.order_by(m.Outbox.created_at, m.Outbox.id).limit(limit).with_for_update(skip_locked=True)
    children = list(db.scalars(query))
    deliveries = {delivery.outbox_id: delivery for delivery in db.scalars(select(m.EmailDelivery).where(
        m.EmailDelivery.outbox_id.in_([child.id for child in children])))} if children else {}
    identity_ids = {}
    for delivery in deliveries.values():
        if delivery.identity_id is not None:
            identity_ids.setdefault(delivery.department_id, set()).add(delivery.identity_id)
    # Keep audience facts within this dispatch; the worker's next transaction must see changed eligibility.
    contexts = {}
    departments = set()
    counts = {}
    for child in children:
        delivery = deliveries.get(child.id)
        if delivery is None:
            child.state, child.last_error = "failed", "EMAIL_DELIVERY_MISSING"
            child.revision += 1
            continue
        config = s.EmailSettings.model_validate(db.get(m.Department, child.department_id).email_settings)
        if counts.get(child.department_id, 0) >= config.batch_size:
            continue
        counts[child.department_id] = counts.get(child.department_id, 0) + 1
        attempted = False
        try:
            if not _enabled(config, delivery.sender_mode):
                raise DeliveryError("EMAIL_DISABLED", draft=True)
            parent = db.get(m.Outbox, delivery.source_outbox_id)
            if child.payload.get('notification_id'):
                identity, receipt = _eligible_notification(db, delivery, parent, child)
            else:
                identity, receipt = _eligible(db, delivery, parent, contexts=contexts,
                                               identity_ids=identity_ids.get(child.department_id, set()))
                if identity.id and pwa.channel_setting(db, child.department_id, identity.id, parent.kind, 'email') == 'off':
                    raise DeliveryError('EMAIL_DISABLED')
            delivery.address = email_messages.address_for(db, identity) or ""
            if not delivery.address:
                raise DeliveryError("EMAIL_ADDRESS_MISSING")
            if not _sender_available(db, delivery):
                continue
            if delivery.sender_mode == "microsoft":
                throttled_until = db.scalar(select(m.Outbox.next_attempt_at).join(m.EmailDelivery,
                    m.EmailDelivery.outbox_id == m.Outbox.id).where(
                    m.Outbox.last_error == "GRAPH_THROTTLED", m.Outbox.next_attempt_at > m.now(),
                    m.EmailDelivery.department_id == child.department_id,
                    m.EmailDelivery.sender_identity_id == delivery.sender_identity_id)
                    .order_by(m.Outbox.next_attempt_at.desc()).limit(1))
                if throttled_until:
                    child.next_attempt_at = throttled_until
                    continue
            if config.send_interval_seconds:
                last = select(m.EmailDelivery.sent_at).where(m.EmailDelivery.department_id == child.department_id,
                    m.EmailDelivery.sender_mode == delivery.sender_mode, m.EmailDelivery.sent_at.is_not(None))
                if delivery.sender_mode == "microsoft":
                    last = last.where(m.EmailDelivery.sender_identity_id == delivery.sender_identity_id)
                sent_at = db.scalar(last.order_by(m.EmailDelivery.sent_at.desc()).limit(1))
                if sent_at and aware(sent_at) + timedelta(seconds=config.send_interval_seconds) > m.now():
                    child.next_attempt_at = aware(sent_at) + timedelta(seconds=config.send_interval_seconds)
                    continue
            child.attempts += 1
            attempted = True
            message = _notification_message(db, child, identity, config, settings) if child.payload.get('notification_id') else email_messages.delivery_message(db, parent, identity.id, settings,
                draft=False, occurrence_id=receipt.event_occurrence_id if receipt else delivery.event_occurrence_id,
                sender_mode=delivery.sender_mode, sender_id=delivery.sender_identity_id,
                person_id=identity.person_id if identity.id is None else None)
            send(db, delivery, message, config, settings)
            child.state, child.last_error = "sent", None
            delivery.state, delivery.error_code, delivery.sent_at = "sent", None, m.now()
        except Exception as exc:
            if isinstance(exc, DeliveryError):
                error = exc
            elif isinstance(exc, Problem) and exc.code == "EMAIL_ORGANISER_ADDRESS_MISSING":
                error = DeliveryError(exc.code, draft=True)
            else:
                error = DeliveryError("EMAIL_DELIVERY_ERROR", retry=True)
            if error.retry and not attempted:
                child.attempts += 1
            if error.retry:
                delay = min(config.retry_max_seconds, config.retry_base_seconds * 2 ** max(0, child.attempts - 1))
                child.next_attempt_at = m.now() + timedelta(seconds=max(delay, error.retry_after))
            if error.retry and child.attempts < config.max_attempts:
                child.state, delivery.state = "retry", "queued"
            else:
                child.state = ("available_in_app" if error.code in {"STALE_EMAIL_SOURCE", "EMAIL_ACCESS_ENDED",
                    "EMAIL_NO_LONGER_PENDING", "EMAIL_DISABLED"} else "failed")
                delivery.state = "draft" if error.draft else "failed"
            child.last_error = delivery.error_code = error.code
            log.warning("email_delivery_failed", extra={"outbox_id": child.id, "error_code": error.code,
                        "attempts": child.attempts, "state": child.state})
        child.revision += 1
        delivery.revision += 1
        departments.add(child.department_id)
        from apps.api import live
        live.signal(db, child.department_id, scope="inbox", id=child.id)
    db.flush()
    return departments
