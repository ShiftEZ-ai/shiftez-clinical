"""Versioned department score weights and advisory tier multipliers."""
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import require_permission, require_role
from apps.api.errors import require
from apps.api.serialization import record
from packages.contracts import schemas as s
from .common import bump_inputs, expected, history, scoped


def definition_from_options(options):
    parsed = s.GenerationOptions.model_validate(options or {})
    return s.ScoreSettingsDefinition(
        weights=parsed.scoring.weights,
        strengths=parsed.scoring.strengths,
    ).model_dump(mode='json')


def latest(db, department_id):
    return db.scalar(select(m.ScoreSettingsVersion).where(
        m.ScoreSettingsVersion.department_id == department_id)
        .order_by(m.ScoreSettingsVersion.revision.desc()).limit(1))


def _append(db, actor, department, definition, reason):
    previous = latest(db, department.id)
    if previous is not None and previous.definition == definition:
        return previous, False
    row = m.ScoreSettingsVersion(
        department_id=department.id,
        revision=previous.revision + 1 if previous else 1,
        definition=definition,
        actor_id=actor.identity_id,
        reason=reason,
        previous_id=previous.id if previous else None,
    )
    db.add(row)
    db.flush()
    history(db, actor, 'score_settings_versions', row, reason)
    return row, True


def sync_projection(db, actor, department, generation_defaults, reason):
    """Record an existing department-settings scoring change without creating a second source of truth."""
    _ensure_projection(db, actor, department)
    return _append(db, actor, department, definition_from_options(generation_defaults), reason)[0]


def _projected(department):
    """Expose the initial version without turning a read into a write."""
    row = m.ScoreSettingsVersion(
        department_id=department.id,
        revision=1,
        definition=definition_from_options(department.generation_defaults),
        actor_id=None,
        reason='Recorded existing score settings',
        previous_id=None,
        effective_from=department.created_at,
    )
    row.id = None
    row.created_at = department.created_at
    return {**record(row), 'stored': False}


def _ensure_projection(db, actor, department):
    """Persist the baseline while the caller holds the department row lock."""
    row = latest(db, department.id)
    if row is None:
        row = _append(db, actor, department, definition_from_options(department.generation_defaults),
                      'Recorded existing score settings')[0]
    return row


def configure(db, actor, command):
    require_permission(actor, 'planning.configure')
    require(command.target_id in (None, actor.department_id), 'NOT_FOUND', 'Department not found', 404)
    department = db.scalar(select(m.Department).where(m.Department.id == actor.department_id).with_for_update())
    previous = latest(db, department.id)
    if previous:
        expected(previous, command.expected_revision)
    else:
        require(command.expected_revision in (None, 1), 'STALE_REVISION',
                'Use the initial score settings revision', 409, {'current_revision': 1})
        previous = _ensure_projection(db, actor, department)
    if command.kind.endswith('.rollback'):
        value = s.ScoreSettingsRollback.model_validate(command.payload)
        source = scoped(db, m.ScoreSettingsVersion, value.version_id, actor)
        definition = source.definition
        reason = value.reason
    else:
        value = s.ScoreSettingsInput.model_validate(command.payload)
        definition = value.definition.model_dump(mode='json')
        reason = value.reason
    definition = s.ScoreSettingsDefinition.model_validate(definition).model_dump(mode='json')
    current = definition_from_options(department.generation_defaults)
    row, changed = _append(db, actor, department, definition, reason)
    if not changed:
        return {'settings': record(row), 'department': record(department), 'undo': None}
    options = s.GenerationOptions.model_validate(department.generation_defaults or {}).model_dump(mode='json')
    scoring = dict(options.get('scoring') or {})
    scoring.update(definition)
    options['scoring'] = s.ScoringSettings.model_validate(scoring).model_dump(mode='json')
    department.generation_defaults = s.GenerationOptions.model_validate(options).model_dump(mode='json')
    department.revision += 1
    bump_inputs(db, actor)
    history(db, actor, 'department_settings', department, reason)
    return {
        'settings': record(row),
        'department': record(department),
        'previous': current,
        'undo': ({'kind': 'score.settings.rollback', 'target_id': department.id,
                  'expected_revision': row.revision,
                  'payload': {'version_id': row.previous_id, 'reason': 'Undo score settings change'}}
                 if row.previous_id else None),
    }


def read(db, actor, *, history_view=False, offset=0, limit=100):
    # Those who change the settings (planning.configure) read them and their history too, not only planners.
    require_role(actor, *s.PERMISSIONS['planning.read'], *s.PERMISSIONS['planning.configure'])
    if history_view:
        rows = list(db.scalars(select(m.ScoreSettingsVersion).where(
            m.ScoreSettingsVersion.department_id == actor.department_id)
            .order_by(m.ScoreSettingsVersion.revision.desc()).offset(offset).limit(limit + 1)))
        if not rows and offset == 0:
            department = db.get(m.Department, actor.department_id)
            return {'versions': [_projected(department)], 'has_more': False}
        return {'versions': [record(row) for row in rows[:limit]], 'has_more': len(rows) > limit}
    department = db.get(m.Department, actor.department_id)
    row = latest(db, actor.department_id)
    return {'settings': record(row) if row else _projected(department),
            'definition': definition_from_options(department.generation_defaults)}
