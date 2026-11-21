"""Dated event occurrences: shared audiences, catalogue history and durable M1 receipts."""
from datetime import date, timedelta
import logging
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select

from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.serialization import json_value, record
from packages.contracts import schemas as s
from packages.domain.recurrence import MAX_PREVIEW_DAYS, expand
from packages.domain.time import aware, local_midnight
from . import audiences, event_delivery, tags
from .common import bump_inputs, expected, history, history_many, scoped

log = logging.getLogger(__name__)


def require_definition_access(actor):
    require_role(actor, 'admin', 'coordinator')


def zone(db, actor):
    return db.get(m.Organisation, actor.organisation_id).timezone


def import_defaults(db, actor, command):
    from .catalogue import write_catalogue
    require_role(actor, 'admin')
    value = s.Decision.model_validate(command.payload)
    require(command.target_id is None, 'EVENT_DEFAULTS_TARGET', 'Import defaults for the current department')
    existing = list(db.scalars(select(m.EventType).where(m.EventType.department_id == actor.department_id)))
    if existing:
        return {'event_types': [record(row) for row in existing], 'imported': 0}
    day = m.now().astimezone(ZoneInfo(zone(db, actor))).date()
    rows = []
    for definition in s.EVENT_TYPE_DEFAULTS:
        created = write_catalogue(db, actor, s.Command(kind='catalogue.create', payload={'catalogue': 'event_types',
            'value': s.EventTypeInput(starts_on=day, **definition).model_dump(mode='json'), 'reason': value.reason}))
        rows.append(created['record'])
    return {'event_types': rows, 'imported': len(rows)}


def event_tag_ids(db, department_id, event_ids):
    result = {event_id: [] for event_id in event_ids}
    query = select(m.TagMember).where(m.TagMember.department_id == department_id,
        m.TagMember.kind == 'events', m.TagMember.member_id.in_(result))
    for member in tags._pending_rows(db, m.TagMember, query, lambda row: row.department_id == department_id
            and row.kind == 'events' and row.member_id in result, department_id):
        result[member.member_id].append(member.tag_id)
    return result


def event_value(db, row):
    value = record(row, exclude=('occurrence_data',))
    value['tag_ids'] = event_tag_ids(db, row.department_id, [row.id])[row.id]
    return value


def check_audience(db, actor, audience):
    from packages.domain.conditions import TAG_FIELDS, absolute_values
    classes = {'person_ids': m.Person, 'category_ids': m.Category, 'service_ids': m.Service,
               'programme_ids': m.Programme, 'capability_ids': m.Capability}
    references = {key: set(getattr(audience, key)) for key in classes}
    lines = list(audience.where.conditions) if audience.where else []
    referenced_tags = {tag.id: tag for tag in tags.department_tags(db, actor.department_id)}
    for line in lines:
        if isinstance(line, s.Conditions):
            lines.extend(line.conditions)
        elif line.field in classes:
            for rid in absolute_values(line) or []:
                tag = referenced_tags.get(rid) if line.field in TAG_FIELDS else None
                if tag:
                    require(TAG_FIELDS[line.field] in tag.labels, 'TAG_KIND', 'The tag does not label this audience field')
                else:
                    references[line.field].add(rid)
    for key, cls in classes.items():
        ids = references[key]
        scope = cls.organisation_id == actor.organisation_id if cls is m.Person else cls.department_id == actor.department_id
        found = set(db.scalars(select(cls.id).where(scope, cls.id.in_(ids)))) if ids else set()
        found -= {row.id for row in db.deleted if isinstance(row, cls)}
        require(ids <= found, 'AUDIENCE_REFERENCE', 'An audience reference is outside this department')


def check_tags(db, actor, ids):
    rows = [row for row in db.scalars(select(m.Tag).where(m.Tag.department_id == actor.department_id,
        m.Tag.id.in_(ids))) if row not in db.deleted] if ids else []
    require({row.id for row in rows} == set(ids) and all(row.active and 'events' in row.labels for row in rows),
            'EVENT_TAG', 'Choose active department tags that label events')


def check_type(db, actor, value):
    check_audience(db, actor, value.default_audience)
    check_tags(db, actor, value.default_tag_ids)


def window_times(value, timezone, horizon, type_end=None, *, starts_on=None):
    if not value.repeat:
        return [{'start': value.start, 'end': value.end}]
    first_day = value.start.astimezone(ZoneInfo(timezone)).date()
    first = max(first_day, starts_on or m.now().astimezone(ZoneInfo(timezone)).date())
    last = min(day for day in (horizon, value.repeat_until, type_end) if day is not None)
    times = []
    while first < last:
        end = min(last, first + timedelta(days=MAX_PREVIEW_DAYS))
        times.extend(timing for timing in expand(value.repeat, first, end)
                     if type_end is None or timing['end'] <= local_midnight(type_end, timezone))
        first = end
    return times


def blocking_input_sources(department_ids):
    """Sources that can make attendance part of scheduling inputs, without a rota window."""
    return {'event_occurrences': (m.EventOccurrence, m.EventOccurrence.department_id.in_(department_ids)
        & m.EventOccurrence.cancelled.is_(False) & m.EventOccurrence.data['type']['blocks_duties'].as_boolean().is_(True)),
        'events': (m.Event, m.Event.department_id.in_(department_ids) & m.Event.cancelled.is_(False)
            & m.Event.occurrence_data['type']['blocks_duties'].as_boolean().is_(True))}


def blocking_sources(department_ids, lo, hi):
    return {'event_occurrences': (m.EventOccurrence, m.EventOccurrence.department_id.in_(department_ids) &
        or_((m.EventOccurrence.start < hi) & (m.EventOccurrence.end > lo),
            (m.EventOccurrence.original_start >= lo - timedelta(days=2)) & (m.EventOccurrence.original_start < hi))),
        'events': (m.Event, blocking_input_sources(department_ids)['events'][1] & (m.Event.start < hi))}


def blocking_occurrences(db, department_ids, lo, hi, *, acquired=None):
    """Project missing series occurrences for this window without changing delivery/history."""
    # Retain moved originals too; an overnight recurrence may exceed 24 hours across DST.
    sources = blocking_sources(department_ids, lo, hi)
    def acquired_rows(name):
        cls, where = sources[name]
        return acquired[name] if acquired is not None else db.scalars(select(cls).where(where))
    stored = list(acquired_rows('event_occurrences'))
    rows = [row for row in stored if not row.cancelled and aware(row.start) < hi and aware(row.end) > lo
            and row.data['type'].get('blocks_duties')]
    existing = {(row.event_id, aware(row.original_start)): row for row in stored}
    active_after = max(lo, m.now())
    for event in acquired_rows('events'):
        if not event.repeat:
            continue
        value = s.EventInput.model_validate({key: aware(getattr(event, key)) if key in {'start', 'end'} else getattr(event, key)
            for key in s.EventInput.model_fields if hasattr(event, key)})
        data = event.occurrence_data
        timezone = data['timezone']
        local_zone = ZoneInfo(timezone)
        # Only recorded occurrences can establish history after they end.
        first = active_after.astimezone(local_zone).date()
        overnight = any(day.weekday == (first.weekday() - 1) % 7 and day.end_time != '24:00'
                        and day.end_time < day.start_time for day in value.repeat.days)
        first -= timedelta(days=int(overnight))
        last = hi.astimezone(local_zone).date()
        if hi != local_midnight(last, timezone):
            last += timedelta(days=1)
        type_end = s.EffectiveInput.model_validate({key: data['type'][key] for key in s.EffectiveInput.model_fields
            if key in data['type']}).ends_on
        for timing in window_times(value, timezone, last, type_end, starts_on=first):
            start, end = aware(timing['start']), aware(timing['end'])
            occurrence = existing.get((event.id, start))
            if start >= hi or end <= active_after or occurrence and (not occurrence.cancelled or occurrence.overridden):
                continue
            # Older bounded edits may have cancelled matching unedited dates; they still block.
            rows.append(SimpleNamespace(id=occurrence.id if occurrence else str(uuid5(NAMESPACE_URL,
                f'fmuc:event-occurrence:{event.id}:{start.isoformat()}')), event_id=event.id,
                department_id=event.department_id, start=start, end=end, revision=event.revision,
                data={**data, 'start': start.isoformat(), 'end': end.isoformat()}))
    return rows


def prepare(db, actor, value, *, saved=None):
    from .email_messages import check_sender
    check_sender(db, actor, value, saved=saved)
    typ = scoped(db, m.EventType, value.event_type_id, actor)
    type_data = record(typ)
    timezone = zone(db, actor)
    if saved and value.cancelled and value.event_type_id == saved.get('event_type_id') and saved.get('type'):
        # Cancellation retains the recorded type and timing even after live configuration changes.
        type_data = saved['type']
        timezone = saved.get('timezone') or timezone
    effective = s.EffectiveInput.model_validate({key: type_data[key] for key in s.EffectiveInput.model_fields if key in type_data})
    start, end = value.start.astimezone(ZoneInfo(timezone)), value.end.astimezone(ZoneInfo(timezone))
    require(effective.active and effective.starts_on <= start.date() and
            (effective.ends_on is None or end <= local_midnight(effective.ends_on, timezone)),
            'EVENT_TYPE_WINDOW', 'The event must lie within its active event type dates')
    require(value.skip_date_set_id is None, 'EVENT_DATE_SET_UNAVAILABLE', 'Date-set exclusions will be available with date sets')
    if value.location_id:
        scoped(db, m.Location, value.location_id, actor)
    people = set(value.organiser_ids) | set(value.presenter_ids)
    if people:
        found = set(db.scalars(select(m.Person.id).where(m.Person.organisation_id == actor.organisation_id, m.Person.id.in_(people))))
        found -= {row.id for row in db.deleted if isinstance(row, m.Person)}
        require(found == people, 'EVENT_PERSON', 'Choose people in this organisation')
    audience = value.audience or s.Audience.model_validate(type_data.get('default_audience', {'everyone': True}))
    check_audience(db, actor, audience)
    tag_ids = value.tag_ids if value.tag_ids is not None else list(type_data.get('default_tag_ids', []))
    check_tags(db, actor, tag_ids)
    value = value.model_copy(update={'audience': audience, 'tag_ids': tag_ids})
    if value.repeat:
        require(value.repeat.timezone == timezone, 'EVENT_TIMEZONE', 'Use the department timezone for recurrence')
        first = expand(value.repeat, start.date(), start.date() + timedelta(days=1))
        require(bool(first) and first[0]['start'] == value.start and first[0]['end'] == value.end,
                'EVENT_REPEAT_START', 'The first recurrence must match the event start and end')
        require(effective.ends_on is None or value.repeat_until is None or value.repeat_until <= effective.ends_on,
                'EVENT_TYPE_WINDOW', 'All occurrences must lie within the event type dates')
    department = db.get(m.Department, actor.department_id)
    horizon = m.now().astimezone(ZoneInfo(timezone)).date() + timedelta(days=department.event_horizon_days)
    times = window_times(value, timezone, horizon, effective.ends_on)
    if value.repeat:
        require(effective.ends_on is None or all(t['end'] <= local_midnight(effective.ends_on, timezone) for t in times),
                'EVENT_TYPE_WINDOW', 'All occurrences must lie within the event type dates')
    data = value.model_dump(mode='json')
    data.update(type=type_data, timezone=timezone, repeat=None, repeat_until=None)
    return value, times, data


def set_tags(db, actor, row, ids, reason):
    current = {r.tag_id: r for r in db.scalars(select(m.TagMember).where(
        m.TagMember.department_id == actor.department_id, m.TagMember.kind == 'events', m.TagMember.member_id == row.id))}
    for tag_id in set(ids) - set(current):
        tags.add_member(db, actor, db.get(m.Tag, tag_id), 'events', row.id, reason)
    for tag_id in set(current) - set(ids):
        tags.remove_member(db, actor, current[tag_id], reason)


def write(db, actor, command, payload, update=False):
    require_role(actor, 'admin', 'coordinator')
    row = scoped(db, m.Event, command.target_id, actor) if update else None
    previous = None
    if row:
        expected(row, command.expected_revision)
        previous = {k: v for k, v in event_value(db, row).items() if k in s.EventInput.model_fields}
    supplied = dict(payload.value)
    if row and 'tag_ids' not in supplied:
        supplied['tag_ids'] = previous['tag_ids']
    if row:
        supplied.update({key: previous[key] for key in ('also_email', 'email_sender', 'email_sender_identity_id')
                         if key not in supplied})
    value, times, data = prepare(db, actor, s.EventInput.model_validate(supplied),
        saved=row.occurrence_data if row else None)
    before_blocks = bool(row and db.scalar(select(m.EventOccurrence.id).where(m.EventOccurrence.event_id == row.id,
        m.EventOccurrence.start >= m.now(),
        m.EventOccurrence.data['type']['blocks_duties'].as_boolean().is_(True)).limit(1)))
    before_blocks = before_blocks or bool(row and row.occurrence_data.get('type', {}).get('blocks_duties'))
    values = value.model_dump(exclude={'tag_ids'})
    values = {k: json_value(v) if k in {'audience', 'repeat'} else v for k, v in values.items()}
    if row:
        for key, val in values.items():
            setattr(row, key, val)
        row.revision += 1
    else:
        row = m.Event(department_id=actor.department_id, **values)
        db.add(row)
    row.occurrence_data = data
    db.flush()
    set_tags(db, actor, row, value.tag_ids, payload.reason)
    changed = materialise(db, row, value, times, data, update=update)
    history_many(db, actor, [('events', row, payload.reason),
        *(('event_occurrence', occurrence, payload.reason) for occurrence in changed)])
    event_delivery.queue(db, actor, row, occurrence_ids=[r.id for r in changed])
    db.get(m.Department, actor.department_id).events_checked_on = None
    if before_blocks or data['type']['blocks_duties']:
        bump_inputs(db, actor, availability=True)
    return {'record': event_value(db, row), 'previous_value': previous, 'occurrence_ids': [r.id for r in changed]}


def materialise(db, row, value, times, data, *, update=False, extend_only=False):
    existing = {aware(r.original_start): r for r in db.scalars(select(m.EventOccurrence).where(m.EventOccurrence.event_id == row.id))}
    if update and not extend_only and value.repeat:
        retained = {start for start, occurrence in existing.items()
            if not occurrence.overridden and aware(occurrence.start) >= m.now()}
        if retained:
            timezone = data['timezone']
            first = min(retained).astimezone(ZoneInfo(timezone)).date()
            last = max(retained).astimezone(ZoneInfo(timezone)).date() + timedelta(days=1)
            type_end = data['type'].get('ends_on')
            included_starts = {aware(timing['start']) for timing in times}
            # The horizon bounds new rows; retained dates still follow actual recurrence edits.
            times = [*times, *(timing for timing in window_times(value, timezone, last,
                date.fromisoformat(type_end) if type_end else None, starts_on=first)
                if aware(timing['start']) in retained and aware(timing['start']) not in included_starts)]
    changed, included = [], set()
    if not value.cancelled and not extend_only:
        for occurrence in existing.values():
            if occurrence.data.get('series_cancelled') and aware(occurrence.start) >= m.now():
                occurrence.cancelled = False
                occurrence.data = {key: val for key, val in occurrence.data.items() if key != 'series_cancelled'}
                occurrence.revision += 1
                changed.append(occurrence)
    for timing in times:
        start, end = aware(timing['start']), aware(timing['end'])
        included.add(start)
        occurrence = existing.get(start)
        if occurrence and ((extend_only and not occurrence.cancelled) or occurrence.overridden or aware(occurrence.start) < m.now()):
            continue
        snapshot = {**data, 'start': start.isoformat(), 'end': end.isoformat()}
        if occurrence:
            occurrence.start, occurrence.end, occurrence.data = start, end, snapshot
            occurrence.cancelled = value.cancelled
            occurrence.revision += 1
        else:
            # A future series revision may not insert newly invented historical occurrences.
            if update and start < m.now():
                continue
            occurrence = m.EventOccurrence(department_id=row.department_id, event_id=row.id, original_start=start,
                start=start, end=end, data=snapshot, cancelled=value.cancelled)
            db.add(occurrence)
        changed.append(occurrence)
    if extend_only:
        return changed
    for start, occurrence in existing.items():
        if start not in included and not occurrence.overridden and aware(occurrence.start) >= m.now() and not occurrence.cancelled:
            occurrence.cancelled = True
            occurrence.revision += 1
            changed.append(occurrence)
    # Cancelling a series also cancels explicitly edited future occurrences.
    if value.cancelled:
        for occurrence in existing.values():
            if not occurrence.cancelled and aware(occurrence.start) >= m.now():
                occurrence.cancelled = True
                if occurrence.overridden:
                    occurrence.data = {**occurrence.data, 'series_cancelled': True}
                occurrence.revision += 1
                changed.append(occurrence)
    return changed


def occurrence_update(db, actor, command):
    require_role(actor, 'admin', 'coordinator')
    payload = s.EventOccurrenceEdit.model_validate(command.payload)
    occurrence = scoped(db, m.EventOccurrence, command.target_id, actor)
    expected(occurrence, command.expected_revision)
    require(aware(occurrence.start) >= m.now(), 'EVENT_HISTORY', 'Past occurrences retain their recorded history', 409)
    require(payload.value.repeat is None, 'EVENT_OCCURRENCE_REPEAT', 'Edit recurrence on the event definition')
    row = scoped(db, m.Event, occurrence.event_id, actor)
    current_tags = event_value(db, row)['tag_ids']
    require(payload.value.tag_ids is None or set(payload.value.tag_ids) == set(current_tags),
            'EVENT_OCCURRENCE_TAGS', 'Edit event labels on the event definition')
    preserved = {key: occurrence.data.get(key) for key in ('also_email', 'email_sender', 'email_sender_identity_id')
                 if key not in payload.value.model_fields_set}
    value, _, data = prepare(db, actor, payload.value.model_copy(update={**preserved, 'tag_ids': current_tags}), saved=occurrence.data)
    require(value.start >= m.now(), 'EVENT_HISTORY', 'An occurrence edit cannot move into recorded history', 409)
    require(not row.cancelled or value.cancelled, 'EVENT_SERIES_CANCELLED', 'Restore the event before reopening an occurrence', 409)
    previous = {k: v for k, v in occurrence.data.items() if k in s.EventInput.model_fields}
    before_blocks = occurrence.data['type']['blocks_duties']
    occurrence.start, occurrence.end, occurrence.data = value.start, value.end, data
    occurrence.overridden, occurrence.cancelled = True, value.cancelled
    occurrence.revision += 1
    row.revision += 1
    history(db, actor, 'event_occurrence', occurrence, payload.reason)
    history(db, actor, 'events', row, payload.reason)
    event_delivery.queue(db, actor, row, occurrence_ids=[occurrence.id])
    db.get(m.Department, actor.department_id).events_checked_on = None
    if before_blocks or data['type']['blocks_duties']:
        bump_inputs(db, actor, availability=True)
    return {'occurrence': record(occurrence), 'previous_value': previous}


def rsvp(db, actor, command):
    value = s.EventRSVP.model_validate(command.payload)
    occurrence = scoped(db, m.EventOccurrence, command.target_id, actor)
    expected(occurrence, command.expected_revision)
    require(not occurrence.cancelled and aware(occurrence.end) > m.now() and occurrence.data['type']['rsvp'],
            'EVENT_RSVP_CLOSED', 'RSVP is closed for this occurrence', 409)
    targets = event_delivery.sync_receipts(db, occurrence, identity_id=actor.identity_id)
    require(actor.identity_id in targets, 'FORBIDDEN', 'This event is outside your audience', 403)
    R = m.AnnouncementReceipt
    receipt = db.scalar(select(R).where(R.event_occurrence_id == occurrence.id, R.identity_id == actor.identity_id))
    maximum = occurrence.data.get('max_attendees')
    if value.response == 'going' and receipt.response != 'going' and maximum is not None:
        going = db.scalar(select(func.count()).select_from(R).where(R.event_occurrence_id == occurrence.id,
            R.identity_id.in_(targets), R.response == 'going'))
        require(going < maximum, 'EVENT_FULL', 'This event has reached its attendee limit', 409)
    if receipt.response != value.response:
        receipt.response, receipt.responded_at = value.response, m.now() if value.response is not None else None
        receipt.revision += 1
        if occurrence.data['type']['blocks_duties']:
            bump_inputs(db, actor, availability=True)
    db.flush()
    return {'receipt': record(receipt)}


def reconcile(db, *, pending_only=False):
    """Extend event windows and reconcile near-term audiences once per department-local day."""
    departments = list(db.scalars(select(m.Department).where(select(m.Event.id).where(
        m.Event.department_id == m.Department.id).exists())))
    changed = set()
    for department in departments:
        if pending_only:
            zone = db.get(m.Organisation, department.organisation_id).timezone
            day = m.now().astimezone(ZoneInfo(zone)).date()
            if department.events_checked_on is None or department.events_checked_on < day:
                return True
            continue
        department_id = department.id
        try:
            with db.begin_nested():
                if _reconcile_department(db, department):
                    changed.add(department_id)
        except Exception as exc:
            log.warning('event_reconcile_failed', extra={'department_id': department_id,
                                                        'error_code': type(exc).__name__})
    return changed


def _reconcile_department(db, department):
    timezone = db.get(m.Organisation, department.organisation_id).timezone
    day = m.now().astimezone(ZoneInfo(timezone)).date()
    if department.events_checked_on is not None and department.events_checked_on >= day:
        return False
    horizon = day + timedelta(days=department.event_horizon_days)
    actor = None
    for row in db.scalars(select(m.Event).where(m.Event.department_id == department.id, m.Event.cancelled.is_(False))):
        value = s.EventInput.model_validate({key: aware(getattr(row, key)) if key in {'start', 'end'} else getattr(row, key)
            for key in s.EventInput.model_fields if hasattr(row, key)})
        data = row.occurrence_data or {**value.model_dump(mode='json'),
            'type': record(db.get(m.EventType, row.event_type_id)), 'timezone': timezone,
            'repeat': None, 'repeat_until': None}
        # Stored snapshots may predate input rules; only effective dates constrain extension.
        effective = s.EffectiveInput.model_validate({key: data['type'][key] for key in s.EffectiveInput.model_fields if key in data['type']})
        times = window_times(value, data['timezone'], horizon, effective.ends_on)
        created = materialise(db, row, value, times, data, update=True, extend_only=True)
        if not created:
            continue
        if actor is None:
            from scripts.system_command import maintenance_identity
            identity = maintenance_identity(db, m, select)
            actor = SimpleNamespace(department_id=department.id, organisation_id=department.organisation_id,
                                    identity_id=identity.id)
        history_many(db, actor, [('event_occurrence', occurrence, 'Daily event window extension') for occurrence in created])
        event_delivery.queue(db, actor, row, [occurrence.id for occurrence in created], key_suffix=f':window:{day}:{horizon}')
        if data['type']['blocks_duties']:
            bump_inputs(db, actor, availability=True)
    near = list(db.scalars(select(m.EventOccurrence).where(m.EventOccurrence.department_id == department.id,
        m.EventOccurrence.cancelled.is_(False), m.EventOccurrence.end > local_midnight(day, timezone),
        m.EventOccurrence.start < local_midnight(day + timedelta(days=department.event_receipt_window_days), timezone))))
    contexts = {}
    for occurrence in near:
        at = event_delivery.local_day(db, occurrence)
        if at not in contexts:
            contexts[at] = audiences.recipient_context(db, department.id, at)
        event_delivery.sync_receipts(db, occurrence, context=contexts[at])
    department.events_checked_on = day
    return True


def counts(db, occurrences, targets):
    R = m.AnnouncementReceipt
    result = {row.id: {'audience': len(targets[row.id]), 'delivered': 0, 'read': 0, 'acknowledged': 0,
                      'going': 0, 'not_going': 0, 'responded': 0} for row in occurrences}
    groups = {}
    for row in occurrences:
        groups.setdefault(frozenset(targets[row.id]), []).append(row.id)
    scopes, size = [], 0

    def collect():
        for oid, delivered, read, acknowledged, going, not_going, responded in db.execute(select(R.event_occurrence_id,
            func.count(R.delivered_at), func.count(R.read_at), func.count(R.acknowledged_at),
            func.count().filter(R.response == 'going'), func.count().filter(R.response == 'not_going'), func.count(R.responded_at))
                .where(or_(*scopes)).group_by(R.event_occurrence_id)):
            for key, count in (('delivered', delivered), ('read', read), ('acknowledged', acknowledged),
                               ('going', going), ('not_going', not_going), ('responded', responded)):
                result[oid][key] += count

    for members, ids in groups.items():
        members = list(members)
        for member_start in range(0, len(members), 15000):
            selected = members[member_start:member_start + 15000]
            maximum = 30000 - len(selected)
            for start in range(0, len(ids), maximum):
                selected_ids = ids[start:start + maximum]
                parameters = len(selected) + len(selected_ids)
                if scopes and (len(scopes) >= 100 or size + parameters > 30000):
                    collect()
                    scopes, size = [], 0
                scopes.append((R.event_occurrence_id.in_(selected_ids)) & R.identity_id.in_(selected))
                size += parameters
    if scopes:
        collect()
    return result


def occurrence_targets(db, actor, rows):
    contexts, targets = {}, {}
    timezone = ZoneInfo(zone(db, actor))
    for row in rows:
        day = aware(row.start).astimezone(timezone).date()
        if day not in contexts:
            contexts[day] = audiences.recipient_context(db, actor.department_id, day)
        targets[row.id] = audiences.recipients(db, actor.department_id, row.data['audience'], day, context=contexts[day])
    return targets


def viewer_occurrence_audiences(db, viewer, starts_on, ends_on):
    """Department occurrences paired with viewer membership; never enumerate all recipients."""
    from .notification_audience import source_visibility
    department = db.get(m.Department, viewer.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    timezone = organisation.timezone
    rows = list(db.scalars(select(m.EventOccurrence).where(m.EventOccurrence.department_id == viewer.department_id,
        source_visibility(db, viewer, m.EventOccurrence),
        m.EventOccurrence.start < local_midnight(ends_on, timezone),
        m.EventOccurrence.end > local_midnight(starts_on, timezone)).order_by(m.EventOccurrence.start, m.EventOccurrence.id)))
    if not rows:
        return []
    labels = audiences.tag_data(db, [viewer.department_id])
    bindings = audiences.role_bindings(db, viewer.department_id, viewer.identity_id)
    people = {viewer.person_id} if viewer.person_id else set()
    context = audiences.profile_context(db, viewer.department_id, department.organisation_id, people)
    contexts, result = {}, []
    for row in rows:
        day = aware(row.start).astimezone(ZoneInfo(timezone)).date()
        if day not in contexts:
            contexts[day] = audiences.viewer(db, viewer, day, context=context, tags=labels, bindings=bindings)
        in_audience = audiences.reaches(db, viewer, row.data.get('audience'), day, seen=contexts[day])
        result.append((row, in_audience))
    return result


def viewer_occurrences(db, viewer, starts_on, ends_on):
    """Personal occurrences, including for planners' calendar feeds."""
    return [row for row, in_audience in viewer_occurrence_audiences(db, viewer, starts_on, ends_on) if in_audience]


def schedule_occurrences(db, actor, starts_on, ends_on):
    """Planners see department events; staff see their audience, without duties or recipient evidence."""
    planner = actor.permits(*s.TEAM_READER_ROLES)
    return [s.ScheduleEventRow(id=row.id, revision=row.revision, event_id=row.event_id,
        start=aware(row.start), end=aware(row.end), cancelled=row.cancelled, in_audience=in_audience,
        attendance=row.data['type']['attendance'], **{key: row.data[key] for key in (
            'event_type_id', 'heading', 'description', 'format', 'location_id', 'place', 'url')}).model_dump(mode='json')
        for row, in_audience in viewer_occurrence_audiences(db, actor, starts_on, ends_on) if planner or in_audience]


def invalidate_audience_inputs(db, actor):
    if db.scalar(select(m.EventOccurrence.id).where(m.EventOccurrence.department_id == actor.department_id,
            m.EventOccurrence.cancelled.is_(False),
            m.EventOccurrence.data['type']['blocks_duties'].as_boolean().is_(True)).limit(1)) or db.scalar(
            select(m.Event.id).where(m.Event.department_id == actor.department_id, m.Event.cancelled.is_(False),
            m.Event.occurrence_data['type']['blocks_duties'].as_boolean().is_(True)).limit(1)):
        bump_inputs(db, actor, availability=True)


def occurrences(db, actor, starts_on, ends_on):
    require(0 < (ends_on - starts_on).days <= s.EVENT_OCCURRENCE_RANGE_MAX_DAYS, 'EVENT_RANGE',
            f'Use a date range of 1 to {s.EVENT_OCCURRENCE_RANGE_MAX_DAYS} days')
    planner = actor.permits(*s.TEAM_READER_ROLES)
    rows = [row for row, in_audience in viewer_occurrence_audiences(db, actor, starts_on, ends_on)
            if planner or in_audience]
    editor = actor.permits('admin', 'coordinator')
    receipts = {r.event_occurrence_id: record(r) for r in db.scalars(select(m.AnnouncementReceipt).where(
        m.AnnouncementReceipt.event_occurrence_id.in_([r.id for r in rows]), m.AnnouncementReceipt.identity_id == actor.identity_id))}
    labels = {}
    for event_id, tag_id in db.execute(select(m.TagMember.member_id, m.TagMember.tag_id).where(
            m.TagMember.department_id == actor.department_id, m.TagMember.kind == 'events',
            m.TagMember.member_id.in_({r.event_id for r in rows}))):
        labels.setdefault(event_id, []).append(tag_id)
    totals = counts(db, rows, occurrence_targets(db, actor, rows)) if editor else {}
    result = []
    for row in rows:
        data, typ = {**row.data, 'tag_ids': labels.get(row.event_id, [])}, row.data['type']
        if not editor:
            data = {key: data[key] for key in ('event_type_id', 'heading', 'description', 'start', 'end',
                'format', 'location_id', 'place', 'url', 'organiser_ids', 'presenter_ids', 'tag_ids',
                'cancelled', 'max_attendees', 'timezone') if key in data}
            typ = {key: typ[key] for key in ('id', 'name', 'colour', 'attendance', 'blocks_duties',
                'calendar', 'rsvp') if key in typ}
        result.append({**record(row), 'data': {**data, 'type': typ}, 'event_type': typ,
            'audience_summary': 'Everyone' if row.data['audience'].get('everyone') else 'Configured audience',
            'receipt': receipts.get(row.id), **({'counts': totals[row.id]} if row.id in totals else {})})
    return {'occurrences': result}


def receipt_list(db, actor, occurrence_id, page, page_size):
    require_role(actor, 'admin', 'coordinator')
    row = scoped(db, m.EventOccurrence, occurrence_id, actor)
    targets = occurrence_targets(db, actor, [row])
    R = m.AnnouncementReceipt
    total = db.scalar(select(func.count()).select_from(R).where(R.event_occurrence_id == row.id))
    query = db.execute(select(R, m.Person.display_name, m.Identity.display_name).join(m.Identity, m.Identity.id == R.identity_id)
        .outerjoin(m.Person, m.Person.id == R.person_id).where(R.event_occurrence_id == row.id)
        .order_by(R.created_at, R.seq).offset((page - 1) * page_size).limit(page_size)).all()
    from .email_messages import receipt_deliveries
    email_deliveries = receipt_deliveries(db, [receipt.id for receipt, _, _ in query])
    return {'receipts': [{**record(receipt), 'person': {'id': receipt.person_id, 'display_name': name} if receipt.person_id else None,
        'identity': {'id': receipt.identity_id, 'display_name': identity_name}, 'in_audience': receipt.identity_id in targets[row.id],
        'email_deliveries': email_deliveries.get(receipt.id, [])}
        for receipt, name, identity_name in query], 'counts': counts(db, [row], targets)[row.id],
        'page': page, 'page_size': page_size, 'total': total}


def attendance_receipts(occurrence_ids):
    from apps.api.snapshots import snapshot_input_row_predicate
    receipt = m.AnnouncementReceipt
    return select(receipt.event_occurrence_id, receipt.person_id).where(
        receipt.event_occurrence_id.in_(occurrence_ids), snapshot_input_row_predicate(receipt))


def protected_facts(db, department_ids, people, lo, hi, zone, *, acquired=None):
    """Derive protected-time facts, resolved by the same catalogue path as approved academic time."""
    from . import audiences
    rows = blocking_occurrences(db, department_ids, lo, hi, acquired=acquired)
    if not rows or not people:
        return []
    going = {}
    for occurrence_id, person_id in db.execute(attendance_receipts([row.id for row in rows])):
        if person_id:
            going.setdefault(occurrence_id, set()).add(person_id)
    contexts, dates, result = {}, {}, []
    for row in rows:
        if row.department_id not in contexts:
            department = db.get(m.Department, row.department_id)
            contexts[row.department_id] = (department.organisation_id, audiences.tag_data(db, [row.department_id]),
                audiences.profile_context(db, row.department_id, department.organisation_id, people),
                audiences.person_role_context(db, row.department_id, people),
                ZoneInfo(db.get(m.Organisation, department.organisation_id).timezone))
        organisation_id, tags, context, role_context, timezone = contexts[row.department_id]
        day = aware(row.start).astimezone(timezone).date()
        key = row.department_id, day
        if key not in dates:
            dates[key] = (audiences.person_roles(db, row.department_id, people, day, context=role_context),
                audiences.profiles(db, row.department_id, organisation_id, set(people), day, context=context))
        roles, profiles = dates[key]
        settings = row.data["type"]
        candidates = going.get(row.id, set()) if settings["attendance"] == "optional" and settings.get("rsvp") else people
        audience = row.data.get("audience") or {"everyone": True}
        if audience.get("everyone"):
            candidates = [pid for pid in candidates if roles.get(pid) or profiles.get(pid, {}).get("category_ids")]
        attendees = []
        for person_id in candidates:
            if person_id in profiles and audiences.matches(audience, roles.get(person_id, set()),
                    person_id, profiles[person_id], tags):
                attendees.append(person_id)
        attendees.sort()
        # Keep each condition within the shared person-list bound, without a fact/query per attendee.
        for offset in range(0, len(attendees), 500):
            fact = {"revision": row.revision, "kind": "protected_time", "status": "active", "on": True,
                "name": row.data["heading"], "event_occurrence_id": row.id,
                "data": {"catalogue_id": "protected_time", "catalogue_version": 1,
                    "interval": {"start": aware(row.start).isoformat(), "end": aware(row.end).isoformat()},
                    "line": {"id": row.id, "who": {"match": "all", "conditions": [
                        {"field": "person_ids", "op": "any", "values": attendees[offset:offset + 500]}]},
                        "numbers": {"apply": {"value": None, "level": "mandatory"}}}}}
            # Attendance protects a person's time across departments; ordinary rules remain department-scoped.
            result.extend({**fact, 'id': f'event:{row.id}:{department_id}:{offset}', 'department_id': department_id}
                          for department_id in department_ids)
    return result
