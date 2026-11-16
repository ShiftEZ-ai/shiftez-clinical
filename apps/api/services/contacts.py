"""Department contact configuration, audited values and the single contact disclosure policy."""
from datetime import date
from typing import Annotated
from urllib.parse import quote
from zoneinfo import ZoneInfo

from pydantic import StringConstraints, TypeAdapter, ValidationError
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import person_access
from apps.api.errors import require
from apps.api.serialization import record
from packages.contracts import schemas as s
from .common import expected, history, scoped, bump_inputs

LEGACY_FIELDS = frozenset(row['legacy_field'] for row in s.DEFAULT_CONTACT_TYPES)


def seed(db, department_id):
    for default in s.DEFAULT_CONTACT_TYPES:
        value = s.ContactTypeInput.model_validate({**{k: v for k, v in default.items() if k not in {'legacy_field', 'id'}},
                                                   'starts_on': date.min})
        db.add(m.ContactType(id=default['id'], department_id=department_id, legacy_field=default['legacy_field'],
                            **value.model_dump(mode='python', exclude={'dial', 'visibility', 'format'}),
                            dial=value.dial.model_dump(), visibility=value.visibility.model_dump(),
                            format=value.format.model_dump()))


def types(db, actor):
    rows = list(db.scalars(select(m.ContactType).where(m.ContactType.department_id == actor.department_id)
                          .order_by(m.ContactType.sort_order, m.ContactType.name, m.ContactType.id).limit(2001)))
    require(len(rows) <= 2000, 'CATALOGUE_LIMIT', 'Page the contact type catalogue before adding more types', 409)
    return rows


def permits(actor, visibility, person_id):
    """Return whether ``actor`` may see a person's contact value.

    A linked person always sees their own value; visibility settings only narrow
    disclosure to other people.
    """
    if actor.person_id is not None and actor.person_id == person_id:
        return True
    mode = visibility.get('mode', 'self_and_admins')
    return (mode == 'everyone' or mode == 'roles' and bool(actor.roles.intersection(visibility.get('roles', [])))
            or mode == 'self_and_admins' and actor.permits('admin'))


def can_edit(actor, typ, person_id):
    return actor.permits(*s.PERMISSIONS['catalogue.department']) or (
        actor.person_id == person_id and typ.edited_by == 'person_and_admins')


def dial_target(typ, value):
    action = s.CONTACT_KIND_ACTIONS.get(typ.kind)
    if action is None or typ.action_off or not value:
        return None
    if action == 'email':
        try:
            TypeAdapter(s.Email).validate_python(value)
        except ValidationError:
            return None
        return 'mailto:' + quote(value, safe='@.+-_')
    # Dial targets never interpret service codes, URI parameters or injected headers.
    import re
    if typ.kind == 'phone':
        number = re.sub(r'[\s().-]', '', value)
        return 'tel:' + number if re.fullmatch(r'\+?[0-9]+', number) else None
    line = typ.dial.get('line', '')
    if re.fullmatch(r'[0-9]{1,15}', value):
        if not line:
            return 'tel:' + value
        if not re.fullmatch(r'\+[1-9][0-9]{1,14}', line):
            return None
        return 'tel:' + line + (',' if typ.dial.get('join') == 'pause' else ';') + value
    return None


def visible_contacts(db, actor, people, *, include_empty=False):
    """Batched reads independent of the number of people; never trust a stored contact projection."""
    people = {p['id']: p for p in people}
    result = {pid: [] for pid in people}
    if not people:
        return result
    today = m.now().astimezone(ZoneInfo(db.get(m.Organisation, actor.organisation_id).timezone)).date()
    configured = types(db, actor)
    # Explicit prior removal remains a disclosure boundary; retain the stored rows and evidence (D67).
    removed = set(db.scalars(select(m.AuditEvent.target_id).where(
        m.AuditEvent.department_id.in_(select(m.Department.id).where(m.Department.organisation_id == actor.organisation_id)),
        m.AuditEvent.action == 'person.anonymise', m.AuditEvent.target_id.in_(people))))
    values = {(row.person_id, row.contact_type_id): row for row in db.scalars(select(m.PersonContact).where(
        m.PersonContact.department_id == actor.department_id, m.PersonContact.person_id.in_(people)))}
    for typ in configured:
        if not typ.active or typ.starts_on > today or typ.ends_on is not None and typ.ends_on <= today:
            continue
        for pid, person in people.items():
            if pid in removed:
                continue
            row = values.get((pid, typ.id))
            # Compatibility for legacy writers/fixtures until all consumers use the typed command.
            value = row.value if row else person.get(typ.legacy_field, '') if typ.legacy_field else ''
            if not value and not include_empty or not permits(actor, typ.visibility, pid):
                continue
            if row and row.visibility_override and typ.person_may_change and not permits(actor, row.visibility_override, pid):
                continue
            result[pid].append({'id': row.id if row else None, 'revision': row.revision if row else None,
                'contact_type_id': typ.id, 'name': typ.name, 'kind': typ.kind, 'icon': typ.icon,
                'sort_order': typ.sort_order, 'value': value, 'dial_target': dial_target(typ, value),
                'can_edit': can_edit(actor, typ, pid), 'legacy_field': typ.legacy_field})
    return result


def validate_value(typ, value):
    require(not typ.required or bool(value), 'CONTACT_REQUIRED', 'This contact type requires a value', 422)
    if not value:
        return
    fmt = s.ContactFormat.model_validate(typ.format)
    try:
        TypeAdapter(Annotated[str, StringConstraints(max_length=fmt.max_length, pattern=fmt.pattern)]).validate_python(value)
    except ValidationError:
        require(False, 'CONTACT_FORMAT', 'The contact does not match this type’s configured format', 422)
    if typ.legacy_field:
        limit = m.Person.__table__.columns[typ.legacy_field].type.length
        require(len(value) <= limit, 'CONTACT_FORMAT', 'This value exceeds the legacy contact field capacity', 422)


def sync_legacy(db, actor, person, fields, reason):
    """Legacy columns are write-through mirrors; copy changes across the organisation's department mappings."""
    configured = list(db.scalars(select(m.ContactType).join(m.Department, m.Department.id == m.ContactType.department_id).where(
        m.Department.organisation_id == actor.organisation_id, m.ContactType.legacy_field.in_(fields))))
    existing = {(r.department_id, r.contact_type_id): r for r in db.scalars(select(m.PersonContact).where(
        m.PersonContact.person_id == person.id, m.PersonContact.contact_type_id.in_([t.id for t in configured])))}
    for typ in configured:
        value = getattr(person, typ.legacy_field)
        row = existing.get((typ.department_id, typ.id))
        if row and row.value == value or row is None and not value:
            continue
        if typ.department_id == actor.department_id:
            validate_value(typ, value)
        if row:
            row.value, row.revision = value, row.revision + 1
        else:
            row = m.PersonContact(person_id=person.id, department_id=typ.department_id, contact_type_id=typ.id, value=value)
            db.add(row)
        from dataclasses import replace
        history(db, replace(actor, department_id=typ.department_id), 'person_contacts', row, reason)


def set_contact(db, actor, command):
    value = s.PersonContactInput.model_validate(command.payload)
    person = person_access(db, actor, value.person_id)
    typ = scoped(db, m.ContactType, value.contact_type_id, actor)
    require(can_edit(actor, typ, person.id), 'FORBIDDEN', 'You cannot edit this contact', 403)
    visible = visible_contacts(db, actor, [record(person)], include_empty=True)
    require(any(c['contact_type_id'] == typ.id for c in visible[person.id]),
            'FORBIDDEN', 'This contact is not visible to you', 403)
    require(typ.active, 'CONTACT_TYPE_INACTIVE', 'Choose an active contact type', 422)
    require(value.visibility_override is None or typ.person_may_change,
            'CONTACT_VISIBILITY', 'This type does not allow a personal visibility override', 422)
    validate_value(typ, value.value)
    row = db.scalar(select(m.PersonContact).where(m.PersonContact.department_id == actor.department_id,
        m.PersonContact.person_id == person.id, m.PersonContact.contact_type_id == typ.id))
    require(command.target_id is None or row is not None and command.target_id == row.id,
            'NOT_FOUND', 'Contact not found', 404)
    previous = {'person_id': person.id, 'contact_type_id': typ.id, 'value': row.value if row else getattr(person, typ.legacy_field) if typ.legacy_field else '',
                'visibility_override': row.visibility_override if row else None, 'reason': value.reason}
    if row:
        expected(row, command.expected_revision)
        row.revision += 1
    else:
        require(command.expected_revision is None, 'STALE_REVISION', 'This contact has not been created', 409)
        row = m.PersonContact(person_id=person.id, department_id=actor.department_id, contact_type_id=typ.id)
        db.add(row)
    row.value = value.value
    row.visibility_override = value.visibility_override.model_dump() if value.visibility_override else None
    history(db, actor, 'person_contacts', row, value.reason)
    if typ.legacy_field and getattr(person, typ.legacy_field) != row.value:
        setattr(person, typ.legacy_field, row.value)
        person.revision += 1
        history(db, actor, 'people', person, value.reason)
        sync_legacy(db, actor, person, {typ.legacy_field}, value.reason)
    bump_inputs(db, actor)
    return {'record': record(row), 'undo': {'kind': 'person.contact.set', 'target_id': row.id,
            'expected_revision': row.revision, 'payload': previous}, 'message': 'Contact saved.'}


def redact_reply(db, actor, result):
    """Reauthorize retained edit/Undo replies as one batch without changing stored evidence."""
    people = set()

    def subject(node, parent=None):
        if node.get('person_id'):
            return node['person_id']
        if node.get('id') and 'display_name' in node:
            return node['id']
        if isinstance(node.get('record'), dict):
            return subject(node['record'], parent)
        if isinstance(node.get('person'), dict) and node['person'].get('id'):
            return node['person']['id']
        if isinstance(node.get('payload'), dict) and node['payload'].get('catalogue') == 'people':
            return node.get('target_id') or parent
        return parent

    def collect(node, parent=None):
        if isinstance(node, dict):
            pid = subject(node, parent)
            if pid:
                people.add(pid)
            for value in node.values():
                collect(value, pid)
        elif isinstance(node, list):
            for value in node:
                collect(value, parent)
    collect(result)
    if not people:
        return result
    rows = [record(p) for p in db.scalars(select(m.Person).where(
        m.Person.organisation_id == actor.organisation_id, m.Person.id.in_(people)))]
    visible = visible_contacts(db, actor, rows, include_empty=True)
    allowed = {pid: {c['contact_type_id'] for c in values} for pid, values in visible.items()}
    fields = {pid: {c['legacy_field'] for c in values} for pid, values in visible.items()}

    def filtered(node, parent=None):
        if isinstance(node, list):
            return [filtered(value, parent) for value in node]
        if not isinstance(node, dict):
            return node
        pid = subject(node, parent)
        hidden = node.get('contact_type_id') and node['contact_type_id'] not in allowed.get(pid, set())
        out = {}
        for key, value in node.items():
            if key in LEGACY_FIELDS and key not in fields.get(pid, set()) or hidden and key == 'value':
                continue
            if key == 'contacts' and isinstance(value, list):
                value = [c for c in value if c.get('contact_type_id') in allowed.get(pid, set())]
            if key == 'previous_values' and isinstance(value, list) and isinstance(node.get('records'), list):
                out[key] = [filtered(before, subject(saved, pid))
                            for before, saved in zip(value, node['records'])]
                continue
            if key == 'undo' and isinstance(value, dict):
                payload = value.get('payload') or {}
                if payload.get('contact_type_id') and payload['contact_type_id'] not in allowed.get(pid, set()):
                    continue
            out[key] = filtered(value, pid)
        return out
    return filtered(result)
