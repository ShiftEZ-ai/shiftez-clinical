"""Bounded, payload-free refusal evidence, independent of the rolled-back command."""
from uuid import UUID, NAMESPACE_URL, uuid5
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from apps.api import models as m
from apps.api.db import transaction


def record_refusal(engine, actor, command, error, key, correlation_id):
    from apps.api.commands import logged_command_kind
    kind = logged_command_kind(command.kind)
    # One immutable refusal per actor/key/command/error; duplicate HTTP attempts cannot grow history.
    ident = str(uuid5(NAMESPACE_URL, f'{actor.department_id}:{actor.identity_id}:{key}:{kind}:{error.code}'))
    payload = command.payload or {}
    target = command.target_id or payload.get('workspace_id') or payload.get('identity_id') or payload.get('person_id')
    try:
        target = str(UUID(target)) if isinstance(target, str) else None
    except ValueError:
        target = None
    attempted_roles = payload.get('roles')
    if not isinstance(attempted_roles, list):
        attempted_roles = [payload['role']] if payload.get('role') else []
    detail = {'error_code': error.code, 'command_kind': kind, 'status': error.status,
              'target_id': target, 'target_kind': kind.split('.', 1)[0],
              'roles': [role for role in attempted_roles if isinstance(role, str)]}
    if isinstance(payload.get('display_name'), str) and payload['display_name']:
        detail['target_label'] = payload['display_name'][:160]
    special = command.kind == 'department.settings' and command.payload.get('score_check_enabled') is True and error.code == 'FORBIDDEN'
    insert = pg_insert if engine.dialect.name == 'postgresql' else sqlite_insert
    # This append needs no scheduling writer lock and touches no business/authority state.
    with transaction(engine) as db:
        db.execute(insert(m.AuditEvent).values(id=ident, department_id=actor.department_id, actor_id=actor.identity_id,
            action='department.score_check_refused' if special else 'command.refused',
            target_id=actor.department_id if special else target,
            reason='Score-check enablement refused' if special else 'Command refused',
            detail=({'error_code': error.code} if special else detail),
            correlation_id=correlation_id).on_conflict_do_nothing(index_elements=['id']))
