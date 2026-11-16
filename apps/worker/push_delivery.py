"""Leased Web Push child outboxes; all external I/O runs after the database closes."""
import base64
import hashlib
import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from email.utils import parsedate_to_datetime
from types import SimpleNamespace

from sqlalchemy import func, or_, select, update

from apps.api import models as m
from apps.api.auth import current_roles, department_roles, session_active, session_limits
from apps.api.data_crypto import decrypt_secret, encrypt_secret
from apps.api.db import retry_write, transaction
from apps.api.errors import Problem
from apps.api.schema import require_current_schema
from apps.api.settings import WORKER_IDLE_MAX_SECONDS
from apps.api.services import branding, pwa
from apps.api.services import pwa_transport as transport
from packages.contracts import schemas as s
from packages.domain.time import aware


def _cancel(event, delivery, reason):
    event.state, event.last_error, delivery.failure_code = 'superseded', reason, reason
    event.revision += 1


def _allowed(db, delivery, subscription, install, settings):
    if pwa.readiness(db, delivery.department_id, settings) != 'ready':
        return 'PUSH_DISABLED'
    identity = db.get(m.Identity, delivery.identity_id)
    if identity is None or identity.disabled or not current_roles(db, identity.id, delivery.department_id):
        return 'PUSH_ACCESS_ENDED'
    if subscription.state != 'active' or install.state != 'enabled' or not db.scalar(select(m.PushEnrollment.id).where(
            m.PushEnrollment.installation_id == install.id, m.PushEnrollment.department_id == delivery.department_id)):
        return 'PUSH_REVOKED'
    if subscription.browser_expiration_at and aware(subscription.browser_expiration_at) <= m.now():
        return 'PUSH_SUBSCRIPTION_EXPIRED'
    if subscription.vapid_key_id not in transport.keyring(settings):
        return 'PUSH_KEY_UNAVAILABLE'
    if delivery.event_id == 'test':
        return None
    source = db.get(m.Outbox, delivery.source_outbox_id)
    if source is not None and pwa.classify_event(source, identity, current_roles(db, identity.id, delivery.department_id)) != delivery.event_id:
        return 'PUSH_EVENT_AUDIENCE_CHANGED'
    if not pwa.channel_allowed(db, delivery.department_id, identity.id, delivery.event_id, 'push'):
        return 'PUSH_OPTED_OUT'
    note = db.get(m.Notification, delivery.notification_id)
    return None if pwa.notification_reaches(db, note) else 'PUSH_ACCESS_ENDED'


def _date_text(db, delivery):
    """Only structured duty dates are eligible; never project arbitrary source strings."""
    source = db.get(m.Outbox, delivery.source_outbox_id)
    identity = db.get(m.Identity, delivery.identity_id)
    if not source or not identity:
        return None
    structured = None
    if source.kind == 'request' and source.payload.get('request_id'):
        row = db.get(m.RequestRecord, source.payload['request_id'])
        structured = row.start if row else None
    elif source.kind == 'event':
        note = db.get(m.Notification, delivery.notification_id)
        row = db.get(m.EventOccurrence, note.source_id) if note and note.source_id else None
        structured = row.start if row else None
    if structured:
        from zoneinfo import ZoneInfo
        department = db.get(m.Department, delivery.department_id)
        zone = db.get(m.Organisation, department.organisation_id).timezone
        return aware(structured).astimezone(ZoneInfo(zone)).date().isoformat()
    changes = (source.payload.get('person_changes') or {}).get(identity.person_id) or {}
    duties = changes.get('added') or changes.get('removed') or []
    if duties and isinstance(duties[0], dict):
        from datetime import datetime
        try:
            value = datetime.fromisoformat(duties[0]['start'])
            return value.date().isoformat()
        except (ValueError, KeyError, TypeError):
            pass
    return None


def payload(db, delivery):
    policy, _ = pwa.policy(db, delivery.department_id)
    preference, _ = pwa.preferences(db, delivery.department_id, delivery.identity_id)
    detail = min([delivery.detail_cap, pwa.detail_cap(policy, preference)], key=pwa.DETAIL.get)
    subscription = db.get(m.PushSubscriptionVersion, delivery.subscription_id)
    install = db.get(m.PushInstallation, subscription.installation_id)
    session = db.get(m.Session, install.session_id) if install.session_id else None
    department = db.get(m.Department, delivery.department_id)
    settings = db.bind.get_execution_options().get('rota_write_settings')
    if session is None or not session_active(session, limits=session_limits(department, settings) if settings else None):
        detail = 'generic'
    title = branding.settings(db)['short_name']
    body = policy['test_message'] if delivery.event_id == 'test' else policy['generic_message']
    if delivery.event_id != 'test' and detail != 'generic':
        title = pwa.CATALOGUE[delivery.event_id]['label']
        date = _date_text(db, delivery) if detail == 'event_date' else None
        body = (date + ' · ' if date else '') + policy['generic_message']
    actor = SimpleNamespace(identity_id=delivery.identity_id, department_id=delivery.department_id)
    return json.dumps({'v': 1, 'notification_id': delivery.notification_id, 'delivery_id': delivery.id,
        'title': title, 'body': body, 'url': '/n/' + delivery.notification_id if delivery.notification_id else '/#inbox',
        'tag': delivery.notification_id or delivery.id, 'unread': pwa.unread(db, actor)}, separators=(',', ':')).encode()


def _authorization(db, settings, config, key_id, origin):
    state = db.get(m.PushProviderState, (origin, key_id))
    if state is None:
        state = m.PushProviderState(origin=origin, key_id=key_id, paused=False)
        db.add(state)
    if state.paused or state.cooldown_until and aware(state.cooldown_until) > m.now():
        return None, state
    context = 'pwa-vapid:' + origin + ':' + key_id
    if state.expires_at and aware(state.expires_at) > m.now() + timedelta(seconds=config.jwt_refresh_margin_seconds):
        return decrypt_secret(state.authorization_ciphertext, context, settings), state
    expiry = m.now() + timedelta(seconds=config.jwt_lifetime_seconds)
    authorization = transport.authorization(settings, key_id, origin, int(expiry.timestamp()))
    state.authorization_ciphertext = encrypt_secret(authorization, context, settings)
    state.expires_at = expiry
    return authorization, state


def claim_batch(db, settings):
    require_current_schema(db)
    config = pwa.system(db)[0]
    if not config.enabled:
        return []
    active_by_department = dict(db.execute(select(m.Outbox.department_id, func.count()).join(
        m.PushTransportLease, m.PushTransportLease.outbox_id == m.Outbox.id).where(
        m.PushTransportLease.until > m.now()).group_by(m.Outbox.department_id)).all())
    slots = max(0, config.max_inflight - sum(active_by_department.values()))
    if not slots:
        return []
    query = select(m.Outbox, m.PushDelivery).join(m.PushDelivery, m.PushDelivery.outbox_id == m.Outbox.id).outerjoin(
        m.PushTransportLease, m.PushTransportLease.outbox_id == m.Outbox.id).where(m.Outbox.kind == 'push',
            m.Outbox.state.in_(('queued', 'retry')), m.Outbox.next_attempt_at <= m.now(),
            or_(m.PushTransportLease.outbox_id.is_(None), m.PushTransportLease.until <= m.now()))
    ranked = query.with_only_columns(m.Outbox.id.label('event_id'), func.row_number().over(
        partition_by=m.Outbox.department_id, order_by=(m.Outbox.next_attempt_at, m.Outbox.id)).label('turn')).subquery()
    rows = db.execute(query.join(ranked, ranked.c.event_id == m.Outbox.id).order_by(ranked.c.turn, m.Outbox.next_attempt_at, m.Outbox.id).limit(min(slots, config.batch_size))
        .with_for_update(skip_locked=True, of=m.Outbox)).all()
    prepared, per_department = [], {}
    for event, delivery in rows:
        policy = pwa.policy(db, delivery.department_id)[0]
        claimed = per_department.get(delivery.department_id, 0)
        if (claimed >= policy['delivery']['batch_size'] or
                claimed + active_by_department.get(delivery.department_id, 0) >= policy['delivery']['concurrency']):
            continue
        per_department[delivery.department_id] = claimed + 1
        if aware(delivery.expires_at) <= m.now():
            _cancel(event, delivery, 'PUSH_EXPIRED'); continue
        subscription = db.get(m.PushSubscriptionVersion, delivery.subscription_id)
        install = db.get(m.PushInstallation, subscription.installation_id)
        denied = _allowed(db, delivery, subscription, install, settings)
        if denied:
            if denied == 'PUSH_SUBSCRIPTION_EXPIRED':
                subscription.state, subscription.revoked_reason = 'expired', 'browser_expired'
                install.state, install.state_reason = 'repair_required', 'subscription_expired'
            _cancel(event, delivery, denied); continue
        try:
            endpoint = decrypt_secret(subscription.endpoint_ciphertext, pwa.context(subscription, 'endpoint'), settings)
            origin = transport.endpoint_origin(endpoint, config)
            authorization, provider = _authorization(db, settings, config, subscription.vapid_key_id, origin)
            if authorization is None:
                event.next_attempt_at = provider.cooldown_until or m.now() + timedelta(seconds=policy['retry']['max_seconds'])
                event.last_error = 'PUSH_PROVIDER_PAUSED' if provider.paused else 'PUSH_PROVIDER_COOLDOWN'
                continue
        except Exception as exc:
            _cancel(event, delivery, getattr(exc, 'code', 'PUSH_PREPARATION_FAILED')); continue
        lease = db.get(m.PushTransportLease, event.id)
        if lease is None:
            lease = m.PushTransportLease(outbox_id=event.id, owner=m.uid(), generation=1,
                                        until=m.now() + timedelta(seconds=config.lease_seconds))
            db.add(lease)
        else:
            lease.owner, lease.generation, lease.until = m.uid(), lease.generation + 1, m.now() + timedelta(seconds=config.lease_seconds)
        event.attempts += 1; event.revision += 1
        subscription.last_attempt_at = m.now()
        prepared.append({'outbox_id': event.id, 'department_id': delivery.department_id, 'generation': lease.generation,
            'owner': lease.owner, 'authorization': authorization,
            'timeout': config.timeout_seconds, 'origin': origin, 'key_id': subscription.vapid_key_id,
            'concurrency': policy['delivery']['concurrency']})
    return prepared


def _retry_after(value):
    if value is None:
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        try:
            return max(0, int((parsedate_to_datetime(value) - m.now()).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return 0


def finish(db, claim, status, retry_after, duration_ms):
    lease = db.get(m.PushTransportLease, claim['outbox_id'])
    if lease is None or lease.owner != claim['owner'] or lease.generation != claim['generation']:
        return False
    event = db.get(m.Outbox, claim['outbox_id'])
    delivery = db.scalar(select(m.PushDelivery).where(m.PushDelivery.outbox_id == event.id))
    subscription = db.get(m.PushSubscriptionVersion, delivery.subscription_id)
    lease.until = m.now()
    if event.state not in ('queued', 'retry'):
        outcome = 'cancelled_in_flight'
    elif status is not None and 200 <= status < 300:
        event.state, event.last_error = 'sent', None
        delivery.accepted_at = subscription.last_accepted_at = m.now()
        subscription.consecutive_failures = 0
        outcome = 'accepted'
    elif status in (404, 410):
        subscription.state, subscription.revoked_reason = 'expired', 'provider_expired'
        install = db.get(m.PushInstallation, subscription.installation_id)
        if not db.scalar(select(m.PushSubscriptionVersion.id).where(m.PushSubscriptionVersion.installation_id == install.id,
                m.PushSubscriptionVersion.state == 'active', m.PushSubscriptionVersion.id != subscription.id)):
            install.state, install.state_reason = 'repair_required', 'subscription_expired'
        siblings = select(m.PushDelivery.outbox_id).where(m.PushDelivery.subscription_id == subscription.id)
        db.execute(update(m.Outbox).where(m.Outbox.id.in_(siblings), m.Outbox.state.in_(('queued', 'retry')))
            .values(state='superseded', last_error='PUSH_SUBSCRIPTION_EXPIRED'))
        event.state, event.last_error = 'failed', 'PUSH_SUBSCRIPTION_EXPIRED'
        outcome = 'subscription_expired'
    elif status is None or status == 429 or status >= 500:
        policy = pwa.policy(db, delivery.department_id)[0]
        retry = policy['retry']
        base = min(retry['max_seconds'], retry['base_seconds'] * 2 ** min(event.attempts - 1, 30))
        # CSPRNG jitter is bounded by the configured delay, shared cooldown is database-owned.
        delay = max(_retry_after(retry_after), base + secrets.randbelow(max(1, base // 4)))
        remaining = max(0, (aware(delivery.expires_at) - m.now()).total_seconds())
        # A provider delay beyond TTL expires the delivery; never overflow datetime or retry early.
        due = m.now() + timedelta(seconds=min(delay, remaining))
        event.next_attempt_at, event.last_error = due, 'PUSH_RATE_LIMITED' if status == 429 else 'PUSH_PROVIDER_UNAVAILABLE'
        if status == 429:
            provider = db.get(m.PushProviderState, (claim['origin'], claim['key_id']))
            provider.cooldown_until = max(aware(provider.cooldown_until) if provider.cooldown_until else m.now(), due)
        event.state = 'retry' if event.attempts < retry['max_attempts'] and delay < remaining else 'failed'
        outcome = 'retrying' if event.state == 'retry' else 'retry_exhausted'
        subscription.consecutive_failures += 1
    else:
        event.state, event.last_error = 'failed', 'PUSH_PROVIDER_' + str(status)
        outcome = 'configuration' if status in (401, 403) else 'protocol'
        if status in (401, 403):
            provider = db.get(m.PushProviderState, (claim['origin'], claim['key_id']))
            provider.paused = True
    delivery.service_status, delivery.failure_code = status, None if outcome == 'accepted' else outcome
    delivery.revision += 1; event.revision += 1
    from apps.api import live
    live.signal(db, event.department_id, scope="inbox", id=event.id)
    db.add(m.PushAttempt(delivery_id=delivery.id, attempt=event.attempts, duration_ms=duration_ms, status=status, outcome=outcome))
    return True


def _send(engine, settings, claim):
    engine = engine.execution_options(rota_defer_busy_writer=False)
    # A fresh projection narrows privacy/access immediately before I/O; no transaction spans the send.
    denied, prepared = None, None
    with transaction(engine) as db:
        lease = db.get(m.PushTransportLease, claim['outbox_id'])
        if lease is None or lease.owner != claim['owner'] or lease.generation != claim['generation']:
            return claim['department_id']
        event = db.get(m.Outbox, claim['outbox_id'])
        delivery = db.scalar(select(m.PushDelivery).where(m.PushDelivery.outbox_id == event.id))
        subscription = db.get(m.PushSubscriptionVersion, delivery.subscription_id)
        install = db.get(m.PushInstallation, subscription.installation_id)
        denied = _allowed(db, delivery, subscription, install, settings)
        if event.state not in ('queued', 'retry') or aware(delivery.expires_at) <= m.now():
            denied = 'PUSH_EXPIRED_OR_CANCELLED'
        provider = db.get(m.PushProviderState, (claim['origin'], claim['key_id']))
        if provider and (provider.paused or provider.cooldown_until and aware(provider.cooldown_until) > m.now()):
            # Release the lease without consuming a network attempt; shared cooldown owns the next claim.
            denied = 'PUSH_PROVIDER_COOLDOWN'
        if not denied:
            try:
                config = pwa.system(db)[0]
                endpoint = decrypt_secret(subscription.endpoint_ciphertext, pwa.context(subscription, 'endpoint'), settings)
                transport.endpoint_origin(endpoint, config)
                keys = {field: decrypt_secret(getattr(subscription, field + '_ciphertext'), pwa.context(subscription, field), settings)
                        for field in ('p256dh', 'auth')}
                policy = pwa.policy(db, delivery.department_id)[0]
                remaining = max(0, int((aware(delivery.expires_at) - m.now()).total_seconds()))
                topic = base64.urlsafe_b64encode(hashlib.sha256((delivery.notification_id or delivery.id).encode()).digest()).decode()[:32]
                urgency = policy['event_defaults'].get(delivery.event_id, {}).get('urgency', 'normal')
                body, headers = transport.encode({'endpoint': endpoint, 'keys': keys}, payload(db, delivery),
                    claim['authorization'], ttl=remaining, topic=topic, urgency=urgency)
                prepared = endpoint, body, headers, min(config.timeout_seconds, WORKER_IDLE_MAX_SECONDS)
            except Exception as exc:
                denied = getattr(exc, 'code', 'PUSH_PREPARATION_FAILED')
    if denied:
        def cancel(db):
            lease = db.get(m.PushTransportLease, claim['outbox_id'])
            if lease is None or lease.owner != claim['owner'] or lease.generation != claim['generation']:
                return
            event = db.get(m.Outbox, claim['outbox_id'])
            delivery = db.scalar(select(m.PushDelivery).where(m.PushDelivery.outbox_id == event.id))
            if denied != 'PUSH_PROVIDER_COOLDOWN':
                _cancel(event, delivery, denied)
                from apps.api import live
                live.signal(db, event.department_id, scope='inbox', id=event.id)
            else:
                event.attempts -= 1
            lease.until = m.now()
        retry_write(engine, cancel)
        return claim['department_id']
    started = time.monotonic()
    try:
        status, retry_after = transport.post(*prepared)
    except Exception:
        status, retry_after = None, None
    elapsed = int((time.monotonic() - started) * 1000)
    retry_write(engine, lambda db: finish(db, claim, status, retry_after, elapsed))
    return claim['department_id']


def dispatch_pending(engine, settings):
    with transaction(engine) as db:
        if db.scalar(select(m.Outbox.id).where(m.Outbox.kind == 'push',
                m.Outbox.state.in_(('queued', 'retry')), m.Outbox.next_attempt_at <= m.now()).limit(1)) is None:
            return set()
    claims = retry_write(engine, lambda db: claim_batch(db, settings))
    if not claims:
        return set()
    with ThreadPoolExecutor(max_workers=len(claims)) as pool:
        return set(pool.map(lambda claim: _send(engine, settings, claim), claims))
