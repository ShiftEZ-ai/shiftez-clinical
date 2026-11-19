"""Versioned PWA configuration, explicit installation consent and privacy projection."""
import base64
import hashlib
import hmac
import io
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageOps
from sqlalchemy import func, select, update
from sqlalchemy.orm import defer

from apps.api import live, models as m
from apps.api.auth import current_roles, require_role
from apps.api.data_crypto import encrypt_secret, decrypt_secret, data_key_from_secret
from apps.api.errors import require
from apps.api.serialization import record
from packages.contracts import schemas as s
from packages.domain.time import aware
from . import branding, notifications, pwa_transport as transport
from .common import expected, history

DETAIL = {value: index for index, value in enumerate(s.PWA_DETAIL_LEVELS)}
CATALOGUE = {event['id']: event for event in s.PWA_NOTIFICATION_EVENTS}


def latest(db, cls, department_id=None, identity_id=None):
    query = select(cls)
    if department_id is not None:
        query = query.where(cls.department_id == department_id)
    if identity_id is not None:
        query = query.where(cls.identity_id == identity_id)
    return db.scalar(query.order_by(cls.revision.desc()).limit(1))


def policy(db, department_id):
    row = latest(db, m.NotificationPolicyVersion, department_id)
    return s.NotificationPolicy.model_validate(row.definition if row else {}).model_dump(mode='json'), row


def system(db):
    row = latest(db, m.PwaSystemVersion)
    return s.PwaSystemSettings.model_validate(row.definition if row else {}), row


def preferences(db, department_id, identity_id):
    row = latest(db, m.NotificationPreferenceVersion, department_id, identity_id)
    return s.NotificationPreferences.model_validate(row.definition if row else {}).model_dump(mode='json'), row


def email_configured(db, department_id):
    settings = s.EmailSettings.model_validate(db.get(m.Department, department_id).email_settings or {})
    return settings.smtp_enabled or settings.m365_enabled


def readiness(db, department_id, settings):
    if not policy(db, department_id)[0]['push_enabled']:
        return 'disabled_by_department'
    if not system(db)[0].enabled:
        return 'disabled_by_system'
    return 'ready' if settings.fmuc_data_key and transport.public_key(settings) and settings.vapid_subject else 'not_configured'


def unread(db, actor):
    return notifications.unread_snapshot(db, actor)


def effective_events(db, actor):
    value, row = policy(db, actor.department_id)
    pref, pref_row = preferences(db, actor.department_id, actor.identity_id)
    email = email_configured(db, actor.department_id)
    events = []
    for event in s.PWA_NOTIFICATION_EVENTS:
        if not actor.permits(*event['roles']):
            continue
        ident = event['id']
        defaults = value['event_defaults'].get(ident, {})
        overrides = pref['event_overrides'].get(ident, {})
        channels = {}
        for channel in ('push', 'email') if email else ('push',):
            state = overrides.get(channel, 'inherit')
            if state == 'inherit':
                state = defaults.get(channel, 'inherit')
            channels[channel] = 'on' if channel == 'push' and event['required'] else state
        events.append({**event, 'channels': channels, 'defaults': defaults,
                       'allowed_channels': ['push', 'email'] if email else ['push']})
    return {'revision': pref_row.revision if pref_row else 1, 'events': events,
            'event_overrides': {key: channels['push'] for key, channels in pref['event_overrides'].items()},
            'event_channels': pref['event_overrides'], 'detail': detail_cap(value, pref) if pref['detail'] else None,
            'detail_cap': value['lock_screen_detail'], 'email_configured': email,
            'policy_revision': row.revision if row else 1}


def installation_rows(db, actor, limit=100, offset=0):
    rows = list(db.scalars(select(m.PushInstallation).where(m.PushInstallation.identity_id == actor.identity_id)
        .order_by(m.PushInstallation.created_at.desc(), m.PushInstallation.id).offset(offset).limit(limit + 1)))
    subscriptions = list(db.scalars(select(m.PushSubscriptionVersion).where(
        m.PushSubscriptionVersion.installation_id.in_([i.id for i in rows]),
        m.PushSubscriptionVersion.state == 'active'))) if rows else []
    active = {sub.installation_id: sub for sub in subscriptions}
    result = []
    for install in rows[:limit]:
        sub = active.get(install.id)
        result.append({'id': install.id, 'revision': install.revision, 'label': install.label, 'platform': install.platform,
            'state': install.state, 'state_reason': install.state_reason, 'last_reconciled_at': install.last_reconciled_at,
            'subscription_id': sub.id if sub else None, 'key_id': sub.vapid_key_id if sub else None,
            'last_accepted_at': sub.last_accepted_at if sub else None})
    return {'installations': result, 'has_more': len(rows) > limit}


def public_policy(value, revision):
    events = {}
    for event in s.PWA_NOTIFICATION_EVENTS:
        channels = value['event_defaults'].get(event['id'], {})
        events[event['id']] = {**event, 'mandatory': event['required'],
            'push': 'on' if event['required'] else channels.get('push', 'inherit'),
            'email': channels.get('email', 'inherit')}
    return {**value, 'revision': revision, 'events': events}


def status(db, actor, settings, limit=100, offset=0):
    value, row = policy(db, actor.department_id)
    revision = row.revision if row else 1
    email = email_configured(db, actor.department_id)
    return s.PwaStatus(enabled=value['push_enabled'], readiness=readiness(db, actor.department_id, settings),
        policy_revision=revision, email_available=email, email_configured=email,
        public_key=transport.public_key(settings), key_id=settings.vapid_key_id or None,
        policy=public_policy(value, revision), **installation_rows(db, actor, limit, offset),
        preferences=effective_events(db, actor), unread=unread(db, actor)).model_dump(mode='json')


def _save_version(db, actor, cls, definition, command, reason, *, personal=False, global_config=False):
    scope = {} if global_config else {'department_id': actor.department_id}
    if personal:
        scope['identity_id'] = actor.identity_id
    previous = latest(db, cls, None if global_config else actor.department_id, actor.identity_id if personal else None)
    expected(previous or SimpleNamespace(revision=1), command.expected_revision or command.payload.get('expected_revision'))
    if previous is None:
        defaults = s.PwaSystemSettings() if global_config else s.NotificationPreferences() if personal else s.NotificationPolicy()
        previous = cls(**scope, definition=defaults.model_dump(mode='json'), actor_id=actor.identity_id,
                       reason='Initial disabled/default configuration', effective_from=m.now(), revision=1)
        db.add(previous); db.flush()
        history(db, actor, cls.__tablename__, previous, previous.reason)
    row = cls(**scope, definition=definition, actor_id=actor.identity_id, reason=reason,
              previous_id=previous.id, effective_from=m.now(), revision=previous.revision + 1)
    db.add(row); db.flush()
    history(db, actor, cls.__tablename__, row, reason)
    return row


def update_policy(db, actor, command):
    require_role(actor, 'admin')
    value = s.NotificationPolicyInput.model_validate(command.payload)
    require(value.department_id == actor.department_id, 'NOT_FOUND', 'Department not found', 404)
    definition = value.model_dump(mode='json', exclude={'department_id', 'expected_revision', 'reason'})
    if not email_configured(db, actor.department_id):
        require(not any(c['email'] == 'on' for c in definition['event_defaults'].values()),
                'EMAIL_NOT_CONFIGURED', 'Configure department email first', 409)
    row = _save_version(db, actor, m.NotificationPolicyVersion, definition, command, value.reason)
    return {'policy': definition, 'revision': row.revision, 'version_id': row.id, 'previous_version_id': row.previous_id}


def update_preferences(db, actor, command):
    value = s.NotificationPreferencesInput.model_validate(command.payload)
    require(value.department_id == actor.department_id, 'NOT_FOUND', 'Department not found', 404)
    cap = policy(db, actor.department_id)[0]['lock_screen_detail']
    require(value.detail is None or DETAIL[value.detail] <= DETAIL[cap], 'PUSH_DETAIL_CAP', 'Choose no more than the department detail level')
    if not email_configured(db, actor.department_id):
        require(not any(c.email == 'on' for c in value.event_overrides.values()), 'EMAIL_NOT_CONFIGURED', 'Configure department email first', 409)
    definition = value.model_dump(mode='json', exclude={'department_id', 'expected_revision', 'reason'})
    previous = preferences(db, actor.department_id, actor.identity_id)[0]
    for event_id, raw in command.payload.get('event_overrides', {}).items():
        if isinstance(raw, str):
            definition['event_overrides'][event_id] = {**previous['event_overrides'].get(event_id, {}),
                **definition['event_overrides'][event_id],
                'email': previous['event_overrides'].get(event_id, {}).get('email', 'inherit')}
    row = _save_version(db, actor, m.NotificationPreferenceVersion, definition, command, value.reason, personal=True)
    return {'preferences': effective_events(db, actor), 'revision': row.revision, 'version_id': row.id,
            'previous_version_id': row.previous_id}


def update_system(db, actor, command):
    require_role(actor, 'operator')
    value = s.PwaSystemInput.model_validate(command.payload)
    row = _save_version(db, actor, m.PwaSystemVersion, value.model_dump(mode='json', exclude={'reason'}),
                        command, value.reason, global_config=True)
    return {'settings': row.definition, 'revision': row.revision, 'version_id': row.id, 'previous_version_id': row.previous_id}


def rollback(db, actor, command):
    value = s.PwaRollbackInput.model_validate(command.payload)
    personal = command.kind == 'notification.preferences.undo'
    require(value.department_id == actor.department_id, 'NOT_FOUND', 'Department not found', 404)
    if not personal:
        require_role(actor, 'admin')
    cls = m.NotificationPreferenceVersion if personal else m.NotificationPolicyVersion
    row = db.get(cls, value.version_id)
    require(row is not None and row.department_id == actor.department_id and
            (not personal or row.identity_id == actor.identity_id), 'NOT_FOUND', 'Configuration version not found', 404)
    new = _save_version(db, actor, cls, row.definition, command, value.reason, personal=personal)
    return {'revision': new.revision, 'version_id': new.id, 'previous_version_id': new.previous_id,
            'preferences' if personal else 'policy': new.definition}


def context(sub, field):
    return f'pwa:{sub.id}:{sub.revision}:{field}'


def _digest(endpoint, settings):
    require(bool(settings.fmuc_data_key), 'PUSH_STORAGE_NOT_CONFIGURED', 'Configure encrypted storage first', 503)
    return hmac.new(data_key_from_secret(settings.fmuc_data_key), endpoint.encode(), hashlib.sha256).hexdigest()


def subscribe(db, actor, command, settings):
    rotation = command.kind == 'push.rotate'
    value = (s.PushRotateInput if rotation else s.PushSubscribeInput).model_validate(command.payload)
    require(value.department_id in (None, actor.department_id), 'NOT_FOUND', 'Department not found', 404)
    require(readiness(db, actor.department_id, settings) == 'ready', 'PUSH_NOT_READY', 'Push is disabled or not configured', 409)
    transport.endpoint_origin(value.endpoint, system(db)[0]); transport.validate_keys(value.keys.model_dump())
    require(value.key_id == settings.vapid_key_id, 'PUSH_KEY_CHANGED', 'Use the current public subscribing key', 409)
    require(value.expiration_time is None or value.expiration_time > m.now(), 'PUSH_EXPIRED', 'The browser subscription has expired', 409)
    digest = _digest(value.endpoint, settings)
    active = db.scalar(select(m.PushSubscriptionVersion).where(m.PushSubscriptionVersion.endpoint_digest == digest,
                                                           m.PushSubscriptionVersion.state == 'active'))
    existing = db.get(m.PushInstallation, active.installation_id) if active else None
    require(existing is None or existing.identity_id == actor.identity_id, 'PUSH_ENDPOINT_OWNED',
            'This browser subscription is bound to another account; unsubscribe in the browser first', 409)
    install = db.scalar(select(m.PushInstallation).where(m.PushInstallation.identity_id == actor.identity_id,
        m.PushInstallation.installation_hint == value.installation_hint))
    if rotation:
        require(install is not None and install.id == value.installation_id, 'NOT_FOUND', 'Installation not found', 404)
        expected(install, value.expected_revision)
    require(not existing or not install or existing.id == install.id, 'PUSH_INSTALLATION_CONFLICT',
            'Use the existing installation for this browser subscription', 409)
    install = install or existing
    if install is None:
        install = m.PushInstallation(identity_id=actor.identity_id, department_id=actor.department_id,
            installation_hint=value.installation_hint, session_id=actor.session_id,
            label=value.label, platform=value.platform, state='enabled', state_reason='', last_reconciled_at=m.now())
        db.add(install); db.flush()
    else:
        install.session_id, install.label, install.platform = actor.session_id, value.label, value.platform
        install.state, install.state_reason, install.last_reconciled_at = 'enabled', '', m.now()
        install.revision += 1
    if not db.scalar(select(m.PushEnrollment.id).where(m.PushEnrollment.installation_id == install.id,
                                                      m.PushEnrollment.department_id == actor.department_id)):
        db.add(m.PushEnrollment(installation_id=install.id, department_id=actor.department_id))
    if active is not None:
        same = active.vapid_key_id == value.key_id and all(decrypt_secret(getattr(active, field + '_ciphertext'),
            context(active, field), settings) == secret for field, secret in
            [('endpoint', value.endpoint), ('p256dh', value.keys.p256dh), ('auth', value.keys.auth)])
        if same:
            active.browser_expiration_at = value.expiration_time
            return {'installation_id': install.id, 'subscription_id': active.id, 'revision': install.revision, 'enabled': True}
    last_revision = db.scalar(select(func.max(m.PushSubscriptionVersion.revision)).where(m.PushSubscriptionVersion.installation_id == install.id)) or 0
    versions = db.scalars(select(m.PushSubscriptionVersion).where(m.PushSubscriptionVersion.installation_id == install.id, m.PushSubscriptionVersion.state == 'active'))
    for old in versions:
        if old.state == 'active':
            old.state, old.revoked_reason = 'superseded', 'subscription_replaced'
    db.flush()
    sub = m.PushSubscriptionVersion(installation_id=install.id, revision=last_revision + 1,
        endpoint_digest=digest, endpoint_ciphertext='', p256dh_ciphertext='', auth_ciphertext='',
        vapid_key_id=value.key_id, browser_expiration_at=value.expiration_time, state='active')
    db.add(sub); db.flush()
    for field, secret in [('endpoint', value.endpoint), ('p256dh', value.keys.p256dh), ('auth', value.keys.auth)]:
        setattr(sub, field + '_ciphertext', encrypt_secret(secret, context(sub, field), settings))
    return {'installation_id': install.id, 'subscription_id': sub.id, 'revision': install.revision, 'enabled': True}


def revoke_installation(db, installation, reason):
    installation.state, installation.state_reason = 'revoked', reason[:120]
    installation.revision += 1
    subscriptions = select(m.PushSubscriptionVersion.id).where(m.PushSubscriptionVersion.installation_id == installation.id)
    pending = select(m.PushDelivery.outbox_id).where(m.PushDelivery.subscription_id.in_(subscriptions))
    db.execute(update(m.Outbox).where(m.Outbox.id.in_(pending), m.Outbox.state.in_(('queued', 'retry')))
               .values(state='superseded', last_error='PUSH_REVOKED'))
    db.execute(update(m.PushSubscriptionVersion).where(m.PushSubscriptionVersion.installation_id == installation.id,
        m.PushSubscriptionVersion.state == 'active').values(state='revoked', revoked_reason=reason[:120]))


def unsubscribe(db, actor, command):
    value = s.PushUnsubscribeInput.model_validate(command.payload)
    install = db.get(m.PushInstallation, value.installation_id)
    require(install is not None and install.identity_id == actor.identity_id, 'NOT_FOUND', 'Installation not found', 404)
    if install.state != 'revoked':
        expected(install, command.expected_revision or value.expected_revision)
        revoke_installation(db, install, value.reason)
    return {'installation_id': install.id, 'revision': install.revision, 'enabled': False}


def classify_event(event, identity, roles=()):
    if event.kind == 'publication' and identity.person_id and (event.payload.get('person_changes') or {}).get(identity.person_id):
        return 'duty_changed'
    if 'coordinator' in roles and event.kind == 'request' and event.payload.get('request_kind') == 'absence_report' and event.payload.get('request_status', 'submitted') == 'submitted':
        return 'absence_reported'
    if 'coordinator' in roles and event.kind in {'absence_report', 'absence_reported'}:
        return 'absence_reported'
    return {'returned': 'review', 'absence_approved': 'availability', 'pattern': 'availability',
            'availability_pattern': 'availability', 'rule_proposal': 'rule'}.get(event.kind, event.kind)


def channel_setting(db, department_id, identity_id, event_id, channel, *, policy_value=None, pref_value=None):
    value = policy_value if policy_value is not None else policy(db, department_id)[0]
    pref = pref_value if pref_value is not None else preferences(db, department_id, identity_id)[0]
    if channel == 'push' and CATALOGUE.get(event_id, {}).get('required'):
        return 'on'
    setting = pref['event_overrides'].get(event_id, {}).get(channel, 'inherit')
    if setting == 'inherit':
        setting = value['event_defaults'].get(event_id, {}).get(channel, 'inherit')
    return setting


def channel_allowed(db, department_id, identity_id, event_id, channel, **values):
    setting = channel_setting(db, department_id, identity_id, event_id, channel, **values)
    # Inherit retains existing email choices and defaults opted-in installations to push.
    return setting == 'on' or setting == 'inherit' and channel == 'push'


def detail_cap(policy_value, pref):
    return min([policy_value['lock_screen_detail'], pref['detail'] or policy_value['lock_screen_detail']], key=DETAIL.get)


def queue_for_event(db, event):
    value, policy_row = policy(db, event.department_id)
    if not value['push_enabled'] or not system(db)[0].enabled:
        return 0
    notes = list(db.scalars(select(m.Notification).where(m.Notification.outbox_id == event.id)))
    if not notes:
        return 0
    identities = {i.id: i for i in db.scalars(select(m.Identity).where(m.Identity.id.in_([n.identity_id for n in notes])))}
    subs = db.execute(select(m.PushSubscriptionVersion, m.PushInstallation).join(m.PushInstallation,
        m.PushInstallation.id == m.PushSubscriptionVersion.installation_id).join(m.PushEnrollment,
        m.PushEnrollment.installation_id == m.PushInstallation.id).where(
            m.PushInstallation.identity_id.in_(identities), m.PushEnrollment.department_id == event.department_id,
            m.PushInstallation.state == 'enabled', m.PushSubscriptionVersion.state == 'active')).all()
    prefs = preference_rows(db, event.department_id, identities)
    existing = set(db.execute(select(m.PushDelivery.notification_id, m.PushDelivery.subscription_id).where(
        m.PushDelivery.source_outbox_id == event.id)).all())
    indexed = {}
    for sub, install in subs:
        indexed.setdefault(install.identity_id, []).append((sub, install))
    count, children, deliveries = 0, [], []
    from apps.api.auth import department_roles
    roles = department_roles(db, event.department_id)
    for note in notes:
        identity = identities[note.identity_id]
        event_id = classify_event(event, identity, roles.get(identity.id, ()))
        if event_id not in CATALOGUE or identity.disabled:
            continue
        pref_row = prefs.get(identity.id)
        pref = s.NotificationPreferences.model_validate(pref_row.definition if pref_row else {}).model_dump(mode='json')
        if not channel_allowed(db, event.department_id, identity.id, event_id, 'push', policy_value=value, pref_value=pref):
            continue
        for sub, install in indexed.get(identity.id, []):
            if (note.id, sub.id) in existing:
                continue
            child = m.Outbox(id=m.uid(), department_id=event.department_id, kind='push', event_key=f'push:{note.id}:{sub.id}',
                             payload={}, next_attempt_at=m.now())
            children.append(child)
            deliveries.append(m.PushDelivery(department_id=event.department_id, outbox_id=child.id, source_outbox_id=event.id,
                notification_id=note.id, identity_id=identity.id, subscription_id=sub.id, event_id=event_id,
                policy_revision=policy_row.revision if policy_row else 1,
                preference_revision=pref_row.revision if pref_row else None, detail_cap=detail_cap(value, pref),
                expires_at=aware(event.created_at) + timedelta(seconds=value['delivery']['ttl_seconds'])))
            count += 1
    db.add_all(children); db.flush()
    db.add_all(deliveries); db.flush()
    return count


def queue_test(db, actor, command, settings):
    value = s.PushTestInput.model_validate(command.payload)
    require(readiness(db, actor.department_id, settings) == 'ready', 'PUSH_NOT_READY', 'Push is disabled or not configured', 409)
    install = db.get(m.PushInstallation, value.installation_id)
    require(install is not None and install.identity_id == actor.identity_id and install.state == 'enabled',
            'NOT_FOUND', 'Enabled installation not found', 404)
    require(db.scalar(select(m.PushEnrollment.id).where(m.PushEnrollment.installation_id == install.id, m.PushEnrollment.department_id == actor.department_id)), 'NOT_FOUND', 'Installation not enabled in this department', 404)
    sub = db.scalar(select(m.PushSubscriptionVersion).where(m.PushSubscriptionVersion.installation_id == install.id,
                                                           m.PushSubscriptionVersion.state == 'active'))
    require(sub is not None, 'PUSH_SUBSCRIPTION_MISSING', 'Enable notifications first', 409)
    interval = system(db)[0].test_interval_seconds
    recent = db.scalar(select(m.PushDelivery.id).where(m.PushDelivery.identity_id == actor.identity_id,
        m.PushDelivery.event_id == 'test', m.PushDelivery.created_at > m.now() - timedelta(seconds=interval)).limit(1))
    require(recent is None, 'PUSH_TEST_RATE', 'Wait before sending another test', 429)
    child = m.Outbox(department_id=actor.department_id, kind='push', event_key='push-test:' + m.uid(), payload={})
    db.add(child); db.flush()
    current, row = policy(db, actor.department_id)
    db.add(m.PushDelivery(department_id=actor.department_id, outbox_id=child.id, source_outbox_id=child.id,
        identity_id=actor.identity_id, subscription_id=sub.id, event_id='test', detail_cap='generic',
        policy_revision=row.revision if row else 1, expires_at=m.now() + timedelta(seconds=current['delivery']['ttl_seconds'])))
    return {'status': 'queued', 'installation_id': install.id, 'outbox_id': child.id}


def current_icons(db, *, render_missing=False):
    value = branding.settings(db)
    source_id = value.get('emblem_image_id')
    uploaded = db.get(m.BrandingImage, source_id, options=[defer(m.BrandingImage.content)]) if source_id else None
    content = None
    if uploaded is None:
        content = (Path(__file__).resolve().parents[3] / 'apps/web/public/assets/brand/emblem.png').read_bytes()
    digest = uploaded.sha256 if uploaded else hashlib.sha256(content).hexdigest()
    profile = {**s.PWA_ICON_PROFILE, 'background': value['icon_background'],
               'fill': value['icon_fill'], 'maskable_fill': value['maskable_fill']}
    profile_id = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()[:40]
    A = m.BrandingIconArtifact
    artifacts = list(db.scalars(select(A).options(defer(A.content)).where(
        A.source_sha256 == digest, A.profile == profile_id)))
    present = {(a.purpose, a.width) for a in artifacts}
    missing = [variant for variant in s.PWA_ICON_PROFILE['variants'] if variant not in present]
    if missing and not render_missing:
        return None
    if missing and uploaded is not None:
        content = uploaded.content
    for purpose, size in missing:
        with Image.open(io.BytesIO(content)) as original:
            source = ImageOps.exif_transpose(original).convert('RGBA')
            fill = value['maskable_fill'] if purpose == 'maskable' else value['icon_fill']
            if purpose == 'maskable':
                # Arbitrary rectangular emblems also fit the 80% safe circle.
                diagonal = (source.width ** 2 + source.height ** 2) ** .5
                scale = min(size * fill / max(source.size), size * .8 / diagonal)
                # Circular transparent emblems can use their whole approved 78% diameter.
                corners = [source.getpixel(p)[3] for p in ((0, 0), (source.width - 1, 0), (0, source.height - 1), (source.width - 1, source.height - 1))]
                if not any(corners):
                    alpha = source.getchannel('A')
                    radius = 0
                    for y in range(source.height):
                        bounds = alpha.crop((0, y, source.width, y + 1)).getbbox()
                        if bounds:
                            radius = max(radius, *( ((x - source.width / 2) ** 2 + (y - source.height / 2) ** 2) ** .5 for x in (bounds[0], bounds[2] - 1)))
                    scale = min(size * fill / max(source.size), size * .4 / max(1, radius))
                target = (max(1, int(source.width * scale)), max(1, int(source.height * scale)))
                image = source.resize(target, Image.Resampling.LANCZOS)
            else:
                image = ImageOps.contain(source, (int(size * fill), int(size * fill)), Image.Resampling.LANCZOS)
            canvas = Image.new('RGBA', (size, size), value['icon_background'])
            canvas.alpha_composite(image, ((size - image.width) // 2, (size - image.height) // 2))
            out = io.BytesIO(); canvas.convert('RGB').save(out, format='PNG')
        binary = out.getvalue()
        sha = hashlib.sha256(binary).hexdigest()
        artifact = m.BrandingIconArtifact(source_image_id=source_id, source_sha256=digest, profile=profile_id,
            purpose=purpose, width=size, height=size, sha256=sha, content=binary)
        db.add(artifact); db.flush()
        artifacts.append(artifact)
    return value, artifacts


def manifest(value, artifacts):
    # Relative same-origin URLs preserve the old effective root identity; Host is never consulted.
    return s.PwaManifest(name=value['app_name'], short_name=value['short_name'], theme_color=value['theme_color'],
        background_color=value['background_color'], icons=[{'src': '/api/pwa/icons/' + a.id + '.png',
            'sizes': f'{a.width}x{a.height}', 'purpose': a.purpose} for a in sorted(artifacts, key=lambda a: (a.purpose, a.width)) if a.purpose != 'apple']).model_dump(mode='json')


def icon(db, artifact_id, *, include_content=True):
    options = () if include_content else (defer(m.BrandingIconArtifact.content),)
    row = db.get(m.BrandingIconArtifact, artifact_id, options=options)
    require(row is not None, 'NOT_FOUND', 'Icon not found', 404)
    if row.source_image_id:
        branding.published_image(db, row.source_image_id, include_content=False)
    return row


def resume_provider(db, actor, command):
    require_role(actor, 'operator')
    value = s.PwaProviderResumeInput.model_validate(command.payload)
    transport.endpoint_origin(value.origin, system(db)[0])
    row = db.get(m.PushProviderState, (value.origin, value.key_id))
    require(row is not None, 'NOT_FOUND', 'Provider state not found', 404)
    row.paused, row.cooldown_until = False, None
    row.authorization_ciphertext, row.expires_at = None, None
    return {'origin': row.origin, 'key_id': row.key_id, 'paused': False}


def versions(db, actor, cls, limit, offset):
    query = select(cls)
    if cls is m.PwaSystemVersion:
        require_role(actor, 'operator')
    else:
        query = query.where(cls.department_id == actor.department_id)
        if cls is m.NotificationPreferenceVersion:
            query = query.where(cls.identity_id == actor.identity_id)
        else:
            require_role(actor, 'admin')
    return {'versions': [record(row) for row in db.scalars(query.order_by(cls.revision.desc()).offset(offset).limit(limit))]}


def destination(db, actor, notification_id):
    rows = notifications._rows(db, actor, m.Notification.id == notification_id, limit=1)
    require(bool(rows), 'NOT_FOUND', 'Notification not found in this department', 404)
    require(notification_reaches(db, db.get(m.Notification, notification_id)), 'NOT_FOUND', 'This notification destination is no longer available', 404)
    # Reuse the inbox's access-redacted link projection; the frontend already routes these links.
    return {'notification_id': notification_id, 'department_id': actor.department_id, 'url': '/#inbox', 'notification': rows[0]}


def preference_rows(db, department_id, identity_ids):
    rank = select(m.NotificationPreferenceVersion.identity_id.label('identity'), func.max(m.NotificationPreferenceVersion.revision).label('revision')).where(
        m.NotificationPreferenceVersion.department_id == department_id,
        m.NotificationPreferenceVersion.identity_id.in_(identity_ids)).group_by(m.NotificationPreferenceVersion.identity_id).subquery()
    pref_rows = db.scalars(select(m.NotificationPreferenceVersion).join(rank,
        (rank.c.identity == m.NotificationPreferenceVersion.identity_id) & (rank.c.revision == m.NotificationPreferenceVersion.revision)).where(
        m.NotificationPreferenceVersion.department_id == department_id))
    prefs = {p.identity_id: p for p in pref_rows}
    return prefs



def notification_reaches(db, note):
    """Current source authorization shared by every external channel and deep-link projection."""
    if note is None:
        return False
    source = db.get(m.Outbox, note.outbox_id)
    identity = db.get(m.Identity, note.identity_id)
    if source is None or source.state not in ('available_in_app', 'queued', 'retry') or identity is None or identity.disabled:
        return False
    from .notification_audience import filter_recipients
    if not filter_recipients(db, source, {identity.id}, {identity.id: identity}, audit=False):
        return False
    roles = current_roles(db, identity.id, source.department_id)
    if not roles:
        return False
    if source.payload.get('workspace_id'):
        workspace = db.get(m.Workspace, source.payload['workspace_id'])
        if workspace:
            from .workspace_access import access_record
            if access_record(db, workspace)['visibility'] == 'private':
                return False
    if source.kind == 'announcement':
        from . import announcements, audiences
        from .independence import today
        item = db.get(m.DepartmentContent, source.payload.get('content_id'))
        day = today(db, source.department_id)
        return bool(item and item.revision == source.payload.get('revision') and announcements.active(item, day) and
            audiences.reaches(db, SimpleNamespace(identity_id=identity.id, person_id=identity.person_id,
                department_id=source.department_id, roles=roles), item.audience, day))
    if source.kind == 'event':
        from . import event_delivery
        occurrence = db.get(m.EventOccurrence, note.source_id) if note.source_id else None
        return bool(occurrence and identity.id in event_delivery.targets(db, occurrence, current_only=True))
    from apps.worker.main import recipients_of
    from apps.api.auth import department_roles
    publication = db.get(m.Publication, source.payload.get('publication_id')) if source.kind == 'publication' else None
    if source.kind == 'publication' and (publication is None or not publication.current):
        return False
    return identity.id in recipients_of(db, source, publication, {identity.id: roles}, identities={identity.id: identity})
