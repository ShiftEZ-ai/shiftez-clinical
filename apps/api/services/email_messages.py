"""Audience/address previews and one message projection for drafts and worker sends."""
from email.utils import formataddr
from types import SimpleNamespace
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.auth import current_roles, require_role
from apps.api.errors import require
from apps.api.serialization import record
from packages.contracts import schemas as s
from packages.domain.email import build_message
from packages.domain.time import aware
from . import audiences
from .common import scoped
from .independence import today


def address_for(db, identity):
    if identity is None or identity.disabled:
        return ''
    person = db.get(m.Person, identity.person_id) if identity.person_id else None
    account = db.scalar(select(m.Account).where(m.Account.identity_id == identity.id)) if identity.id else None
    return _address(identity, person, account)


def _address(identity, person, account):
    if identity is None or identity.disabled:
        return ''
    value = (person.work_email if person else '') or (account.email if account else '')
    try:
        return TypeAdapter(s.Email).validate_python(value) if value else ''
    except ValidationError:
        return ''


def sender_identity_id(db, row, parent):
    data = row.data if isinstance(row, m.EventOccurrence) else record(row)
    explicit = data.get('email_sender_identity_id')
    if explicit:
        return explicit
    config = s.EmailSettings.model_validate(db.get(m.Department, row.department_id).email_settings)
    if (data.get('email_sender') or config.default_sender) == 'microsoft':
        return None  # Existing SMTP items need their own organiser to select a mailbox before switching channels.
    for person_id in data.get('organiser_ids', []):
        for ident in db.scalars(select(m.Identity).where(m.Identity.person_id == person_id, m.Identity.disabled.is_(False))):
            if current_roles(db, ident.id, row.department_id) & {'admin', 'coordinator'}:
                return ident.id
    return parent.payload.get('actor_id')


def check_sender(db, actor, value, *, saved=None):
    # Maintaining a saved selection does not choose another editor's mailbox anew.
    unchanged = saved is not None and all(getattr(value, key) == saved.get(key)
        for key in ('email_sender', 'email_sender_identity_id'))
    config = s.EmailSettings.model_validate(db.get(m.Department, actor.department_id).email_settings)
    if not unchanged and (value.email_sender or config.default_sender) == 'microsoft' and value.email_sender_identity_id is None:
        value.email_sender_identity_id = actor.identity_id
    ident = value.email_sender_identity_id
    if ident:
        identity = db.get(m.Identity, ident)
        if not unchanged:
            require(identity is not None and not identity.disabled and ident == actor.identity_id,
                    'EMAIL_SENDER', 'Choose your own mailbox; another organiser must connect and send their own email', 403)
        if isinstance(value, s.EventInput) and value.organiser_ids:
            require(identity is not None and identity.person_id in value.organiser_ids,
                    'EMAIL_ORGANISER', 'The mailbox sender must be an organiser')
    if value.email_sender:
        require(config.smtp_enabled and config.m365_enabled, 'EMAIL_SENDER_MODE',
                'A per-item sender override needs both department sender modes configured', 409)


def calendar_event(db, row, *, locations=None):
    value = row.data
    lid = value.get('location_id')
    location = (locations.get(lid) if locations is not None else db.get(m.Location, lid)) if lid else None
    place = ', '.join(dict.fromkeys(p for p in (location.name if location else '', value.get('place')) if p))
    return {'id': row.id, 'revision': row.revision, 'start': row.start, 'end': row.end,
            'heading': value['heading'], 'description': value.get('description', ''), 'place': place,
            'online_url': value.get('url', ''), 'attendance': value['type']['attendance'], 'cancelled': row.cancelled}


def _source(db, actor, kind, rid):
    require_role(actor, 'admin', 'coordinator')
    if kind == 'content':
        row = scoped(db, m.DepartmentContent, rid, actor)
        require(row.kind == 'announcement', 'EMAIL_SOURCE', 'Only announcements have email drafts')
        return row, []
    row = db.get(m.Event, rid)
    if row is not None:
        scoped(db, m.Event, row.id, actor)
        return row, list(db.scalars(select(m.EventOccurrence).where(m.EventOccurrence.event_id == row.id)
                                   .order_by(m.EventOccurrence.start, m.EventOccurrence.id)))
    occurrence = scoped(db, m.EventOccurrence, rid, actor)
    return db.get(m.Event, occurrence.event_id), [occurrence]


def _audience_targets(db, actor, row, occurrences):
    """One request-local projection; reuse facts while evaluating each occurrence's own date."""
    from .email_audiences import extra_context, extra_people
    from .event_delivery import targets as occurrence_targets
    facts = audiences.recipient_facts(db, actor.department_id)
    extra_facts = extra_context(db, actor.department_id)
    if isinstance(row, m.DepartmentContent):
        day = today(db, actor.department_id)
        current = (audiences.recipients(db, actor.department_id, row.audience, day,
            context=audiences.recipient_context(db, actor.department_id, day, facts=facts)),
            extra_people(db, actor.department_id, row.audience, day, context=extra_facts))
        return {row.id: {False: current, True: current}}
    cancelled = {r.id for r in occurrences if r.cancelled}
    previous = {rid: (set(), set()) for rid in cancelled}
    previous_people = {rid: set() for rid in cancelled}
    if cancelled:
        for rid, iid in db.execute(select(m.AnnouncementReceipt.event_occurrence_id, m.AnnouncementReceipt.identity_id)
                .where(m.AnnouncementReceipt.event_occurrence_id.in_(cancelled))):
            previous[rid][0].add(iid)
        for rid, pid, iid in db.execute(select(m.EmailDelivery.event_occurrence_id,
                m.EmailDelivery.person_id, m.EmailDelivery.identity_id).where(
                    m.EmailDelivery.event_occurrence_id.in_(cancelled), m.EmailDelivery.person_id.is_not(None))):
            previous[rid][1].add(pid)
            if iid is None:
                previous_people[rid].add(pid)
    zone = ZoneInfo(facts[2]['organisation']['timezone'])
    contexts, result = {}, {}
    for occurrence in occurrences:
        day = aware(occurrence.start).astimezone(zone).date()
        if day not in contexts:
            contexts[day] = audiences.recipient_context(db, actor.department_id, day, facts=facts)
        context = contexts[day]
        current = (occurrence_targets(db, occurrence, context, current_only=True),
            extra_people(db, actor.department_id, occurrence.data['audience'], day, context=extra_facts))
        targets, people = current
        if occurrence.cancelled:
            targets = occurrence_targets(db, occurrence, context, previous=previous[occurrence.id])
            if previous_people[occurrence.id]:
                people = list({p.id: p for p in [*people, *extra_people(db, actor.department_id,
                    {'person_ids': list(previous_people[occurrence.id])}, day, context=extra_facts)]}.values())
        result[occurrence.id] = {False: (targets, people), True: current}
    return result


def _targets(prepared, occurrence_ids=None, *, current_only=False):
    targets = set()
    for rid in prepared if occurrence_ids is None else occurrence_ids:
        targets |= prepared[rid][current_only][0]
    return targets


def _extra_targets(prepared, occurrence_ids=None, *, current_only=False):
    people = {}
    for rid in prepared if occurrence_ids is None else occurrence_ids:
        people.update({p.id: p for p in prepared[rid][current_only][1]})
    return list(people.values())


def preview(db, actor, kind, rid, settings, page=1, page_size=50):
    row, occurrences = _source(db, actor, kind, rid)
    config = s.EmailSettings.model_validate(db.get(m.Department, actor.department_id).email_settings)
    prepared = _audience_targets(db, actor, row, occurrences)
    return _preview(db, actor, kind, rid, settings, row, occurrences, config, prepared, page, page_size)


def _preview(db, actor, kind, rid, settings, row, occurrences, config, prepared, page=1, page_size=50):
    recipients, missing = [], []
    contacts = select(m.Identity, m.Person, m.Account).outerjoin(m.Person, m.Person.id == m.Identity.person_id) \
        .outerjoin(m.Account, m.Account.identity_id == m.Identity.id) \
        .where(m.Identity.id.in_(_targets(prepared))).order_by(m.Identity.id)
    for identity, person, account in db.execute(contacts):
        address = _address(identity, person, account)
        item = {'identity_id': identity.id, 'person_id': identity.person_id,
                'display_name': person.display_name if person else identity.display_name}
        (recipients if address else missing).append({**item, **({'address': address} if address else {})})
    for person in _extra_targets(prepared):
        address = _address(SimpleNamespace(disabled=False), person, None)
        item = {'identity_id': None, 'person_id': person.id, 'display_name': person.display_name}
        (recipients if address else missing).append({**item, **({'address': address} if address else {})})
    source = occurrences[0] if occurrences else row
    source_data = source.data if isinstance(source, m.EventOccurrence) else record(source)
    mode = source_data.get('email_sender') or config.default_sender
    sender_id = sender_identity_id(db, occurrences[0] if occurrences else row, SimpleNamespace(payload={'actor_id': actor.identity_id}))
    message = ''
    if mode == 'microsoft':
        from . import mailboxes
        if not config.m365_enabled or not mailboxes.connected(db, sender_id, actor.department_id, settings):
            message = 'Connect your Microsoft mailbox to send automatically, or download the email draft. No other sender is used.'
    elif not config.smtp_enabled:
        message = 'Department email is off. Download the email draft to send it yourself.'
    if mode == 'microsoft' and occurrences and not address_for(db, db.get(m.Identity, sender_id) if sender_id else None):
        message = 'Select your own mailbox and add your work or linked account email before automatic invitations. Download the draft to complete it yourself.'
    source_key = m.Outbox.payload['content_id' if kind == 'content' else 'event_id'].as_string()
    delivery_query = select(m.EmailDelivery).join(m.Outbox, m.Outbox.id == m.EmailDelivery.source_outbox_id).where(
        m.EmailDelivery.department_id == actor.department_id, source_key == row.id)
    if kind == 'event' and rid != row.id:
        delivery_query = delivery_query.where((m.EmailDelivery.event_occurrence_id == rid) |
            m.EmailDelivery.receipt_id.in_(select(m.AnnouncementReceipt.id).where(m.AnnouncementReceipt.event_occurrence_id == rid)))
    delivery_total = db.scalar(select(func.count()).select_from(delivery_query.subquery()))
    deliveries = list(db.scalars(delivery_query.order_by(m.EmailDelivery.created_at.desc(), m.EmailDelivery.seq.desc())
        .offset((page - 1) * page_size).limit(page_size)))
    return {'addresses': sorted({r['address'] for r in recipients}), 'recipients': recipients, 'missing_addresses': missing,
            'recipient_header': config.recipient_header, 'sender_mode': mode, 'sender_identity_id': sender_id,
            'draft_url': f'/api/{"content" if kind == "content" else "events"}/{rid}/draft.eml', 'message': message,
            'email_deliveries': [{**record(d), 'channel': 'email'} for d in deliveries],
            'delivery_page': page, 'delivery_page_size': page_size, 'delivery_total': delivery_total}


def _message(db, row, occurrences, addresses, sender_id, config, settings, *, draft, include_invite=True, sender_mode=None, redacted=False):
    sender = db.get(m.Identity, sender_id) if sender_id else None
    organiser_address = address_for(db, sender)
    source_data = occurrences[0].data if len(occurrences) == 1 else record(row)
    mode = sender_mode or source_data.get('email_sender') or config.default_sender
    require(draft or not (occurrences and include_invite and mode == 'microsoft' and not organiser_address),
            'EMAIL_ORGANISER_ADDRESS_MISSING', 'Add the organiser work email or download and complete the email draft', 422)
    sender_address = config.sender_address if mode == 'smtp' else organiser_address
    sender_name = config.sender_name if mode == 'smtp' else (sender.display_name if sender else '')
    name = row.name if isinstance(row, m.DepartmentContent) else row.heading
    details = row.description
    if len(occurrences) == 1:
        name = occurrences[0].data['heading']
        details = occurrences[0].data.get('description', '')
    location_ids = {r.data['location_id'] for r in occurrences if r.data.get('location_id')}
    locations = {r.id: r for r in db.scalars(select(m.Location).where(m.Location.id.in_(location_ids)))} if location_ids else {}
    invitations = [calendar_event(db, r, locations=locations) for r in occurrences]
    if redacted:
        name, details = 'Cancelled: Event', 'An event previously shared with you has been cancelled.'
        for invitation in invitations:
            invitation.update(heading='Event', description=details, place='', online_url='')
    elif occurrences:
        lines = []
        for data in invitations:
            lines.append(f"{'Cancelled: ' if data['cancelled'] else ''}{data['start'].isoformat()} – {data['end'].isoformat()}\n"
                         f"{data['place']}\n{data['online_url']}")
        details += '\n\n' + '\n\n'.join(lines)
    elif isinstance(row, m.DepartmentContent) and row.url:
        details += '\n' + row.url
    target = {'content': row.id} if isinstance(row, m.DepartmentContent) else {'event': occurrences[0].id if len(occurrences) == 1 else row.id}
    path = '/#home?' + urlencode(target)
    return build_message(heading=name, details=details, acknowledge_url=settings.public_origin + path,
        recipients=addresses, recipient_header=config.recipient_header,
        sender=formataddr((sender_name, sender_address)) if sender_address and (mode == 'smtp' or draft) else None,
        reply_to=config.reply_to or organiser_address or None, draft=draft,
        organiser=(organiser_address or None) if mode == 'microsoft' else (organiser_address or config.sender_address or None),
        events=invitations if include_invite else [])


def draft(db, actor, kind, rid, settings, *, group=None):
    row, occurrences = _source(db, actor, kind, rid)
    config = s.EmailSettings.model_validate(db.get(m.Department, actor.department_id).email_settings)
    if len(occurrences) > 1:
        selections = {(r.data.get('email_sender') or config.default_sender,
                       r.data.get('email_sender_identity_id')) for r in occurrences}
        require(len(selections) == 1, 'EMAIL_SENDERS_DIFFER',
                'These occurrences have different senders. Download each occurrence draft separately.', 409,
                details={'draft_urls': [f'/api/events/{r.id}/draft.eml' for r in occurrences]})
    prepared = _audience_targets(db, actor, row, occurrences)
    if len(occurrences) > 1:
        for current_only in (False, True):
            recipient_sets = [{*('identity:' + iid for iid in _targets(prepared, [r.id], current_only=current_only)),
                               *('person:' + p.id for p in _extra_targets(prepared, [r.id], current_only=current_only))}
                              for r in occurrences]
            require(all(targets == recipient_sets[0] for targets in recipient_sets), 'EMAIL_AUDIENCES_DIFFER',
                    'These occurrences have different audiences. Download each occurrence draft separately.', 409,
                    details={'draft_urls': [f'/api/events/{r.id}/draft.eml' for r in occurrences]})
    info = _preview(db, actor, kind, rid, settings, row, occurrences, config, prepared)
    current_ids = _targets(prepared, current_only=True)
    current_people = {p.id for p in _extra_targets(prepared, current_only=True)}
    current_addresses = {r['address'] for r in info['recipients'] if r['identity_id'] in current_ids
                         or (r['identity_id'] is None and r['person_id'] in current_people)}
    former_addresses = set(info['addresses']) - current_addresses
    has_former = (_targets(prepared) != current_ids
                  or {p.id for p in _extra_targets(prepared)} != current_people)
    selected = group or ('current' if current_addresses or not has_former else 'former')
    addresses = sorted(former_addresses if selected == 'former' else current_addresses)
    require(group is None or addresses, 'EMAIL_DRAFT_GROUP_EMPTY', 'This draft group has no email recipients', 404)
    return _message(db, row, occurrences, addresses, info['sender_identity_id'], config, settings,
                    draft=True, sender_mode=info['sender_mode'], redacted=selected == 'former')


def delivery_message(db, event, identity_id, settings, draft=False, occurrence_id=None, sender_mode=None, sender_id=None, person_id=None):
    config = s.EmailSettings.model_validate(db.get(m.Department, event.department_id).email_settings)
    if event.kind == 'announcement':
        row, occurrences = db.get(m.DepartmentContent, event.payload['content_id']), []
    else:
        row = db.get(m.Event, event.payload['event_id'])
        query = select(m.EventOccurrence).where(m.EventOccurrence.event_id == row.id)
        occurrences = list(db.scalars(query.where(m.EventOccurrence.id == occurrence_id))) if occurrence_id else list(db.scalars(query))
    source = occurrences[0] if occurrences else row
    sender_id = sender_id if sender_id is not None else sender_identity_id(db, source, event)
    recipient = db.get(m.Identity, identity_id) if identity_id else SimpleNamespace(id=None, person_id=person_id, disabled=False)
    from . import email_audiences, event_delivery
    redacted = any(r.cancelled and (
        identity_id not in event_delivery.targets(db, r, current_only=True) if identity_id else
        person_id not in {p.id for p in email_audiences.extra_people(db, event.department_id,
            r.data.get('audience'), event_delivery.local_day(db, r))}) for r in occurrences)
    invite = True
    if identity_id and config.event_invites == 'without_calendar_link':
        invite = db.scalar(select(m.CalendarFeed.id).where(m.CalendarFeed.department_id == event.department_id,
            m.CalendarFeed.identity_id == identity_id, m.CalendarFeed.revoked.is_(False),
            m.CalendarFeed.person_id == recipient.person_id).limit(1)) is None
    return _message(db, row, occurrences, [address_for(db, recipient)], sender_id, config, settings,
                    draft=draft, include_invite=invite, sender_mode=sender_mode, redacted=redacted)


def receipt_deliveries(db, receipt_ids):
    result = {}
    for delivery in db.scalars(select(m.EmailDelivery).where(m.EmailDelivery.receipt_id.in_(receipt_ids)).order_by(m.EmailDelivery.created_at, m.EmailDelivery.seq)):
        result.setdefault(delivery.receipt_id, []).append({
            'id': delivery.id, 'channel': 'email', 'sender_mode': delivery.sender_mode, 'state': delivery.state,
            'sent_at': delivery.sent_at, 'error_code': delivery.error_code,
            'draft_available': delivery.state in {'failed', 'draft'}})
    return result
