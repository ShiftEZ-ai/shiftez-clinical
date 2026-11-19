"""Append-only department display definitions; departments retain the active projection."""
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.serialization import record
from packages.contracts import schemas as s
from .common import expected, history, scoped

# A department created before the catalogue became the default stores no value: it shows the current Rules page.
STORED_BEFORE_DEFAULTS = {'rule_catalogue': False}


def latest(db, department_id):
    return db.scalar(select(m.DisplaySettingsVersion).where(
        m.DisplaySettingsVersion.department_id == department_id)
        .order_by(m.DisplaySettingsVersion.revision.desc()).limit(1))


def append(db, actor, department, definition, reason):
    previous = latest(db, department.id)
    if previous is None:
        previous = m.DisplaySettingsVersion(department_id=department.id, revision=1,
            definition=dict(department.display_settings or {}), actor_id=actor.identity_id,
            reason='Recorded existing display settings')
        db.add(previous)
        db.flush()
    row = m.DisplaySettingsVersion(department_id=department.id, revision=previous.revision + 1,
        definition=definition, actor_id=actor.identity_id, reason=reason, previous_id=previous.id)
    db.add(row)
    department.display_settings = definition
    db.flush()
    history(db, actor, 'display_settings_versions', row, reason)
    return row


def change(db, actor, command):
    require_role(actor, 'admin')
    require(command.target_id == actor.department_id, 'NOT_FOUND', 'Department not found', 404)
    department = db.scalar(select(m.Department).where(m.Department.id == actor.department_id).with_for_update())
    expected(department, command.expected_revision)
    stored = dict(department.display_settings or {})
    previous = {key: value for key, value in stored.items() if key in s.DisplaySettingsInput.model_fields}
    current = {**STORED_BEFORE_DEFAULTS, **stored}
    if command.kind == 'department.display.restore':
        value = s.DisplaySettingsRestore.model_validate(command.payload)
        source = scoped(db, m.DisplaySettingsVersion, value.version_id, actor)
        # Only restore display form fields, preserving newer unrelated controls in the shared projection.
        restored = s.DisplaySettingsInput.model_validate({
            **{key: val for key, val in {**STORED_BEFORE_DEFAULTS, **source.definition}.items()
               if key in s.DisplaySettingsInput.model_fields},
            'reason': value.reason}).model_dump(mode='json', exclude={'reason'})
        definition = {**current, **restored}
    else:
        value = s.DisplaySettingsInput.model_validate(command.payload, context={'label_write': True})
        definition = {**current, **{key: val if key in value.model_fields_set or key not in current else current[key]
            for key, val in value.model_dump(mode='json', exclude={'reason'}).items()}}
    row = append(db, actor, department, definition, value.reason)
    department.revision += 1
    history(db, actor, 'department_display', department, value.reason)
    return {'department': record(department), 'previous': previous, 'version_id': row.id,
            'previous_version_id': row.previous_id, 'display_revision': row.revision,
            'undo': {'kind': 'department.display.restore', 'target_id': department.id,
                     'expected_revision': department.revision,
                     'payload': {'version_id': row.previous_id, 'reason': 'Undo display settings change'}}}


def versions(db, actor, limit, cursor):
    require_role(actor, 'admin')
    V = m.DisplaySettingsVersion
    rows = list(db.scalars(select(V).where(V.department_id == actor.department_id,
        *([V.revision < cursor] if cursor is not None else [])).order_by(V.revision.desc()).limit(limit + 1)))
    page = rows[:limit]
    names = dict(db.execute(select(m.Identity.id, m.Identity.display_name).where(
        m.Identity.id.in_({row.actor_id for row in page if row.actor_id}))).all())
    predecessors = {row.id: row.definition for row in db.scalars(select(V).where(
        V.department_id == actor.department_id, V.id.in_({row.previous_id for row in page if row.previous_id})))}
    return {'versions': [{**record(row), 'actor_display_name': names.get(row.actor_id),
        'changed_fields': sorted(key for key in row.definition.keys() | predecessors.get(row.previous_id, {}).keys()
                                if row.definition.get(key) != predecessors.get(row.previous_id, {}).get(key))}
        for row in page], 'next_cursor': page[-1].revision if len(rows) > limit else None}
