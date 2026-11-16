#!/usr/bin/env python3
"""Remove private rule metadata through additive, audited revisions; dry-run by default.

Run with the environment's DATABASE_URL, --department <id> and --as <admin>.
Apply only after reviewing the dry-run: --apply --reason "Approved metadata repair".
Historical definitions, conversion evidence and publications are never rewritten.
"""
import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import select  # noqa: E402
from apps.api import models as m  # noqa: E402
from apps.api.auth import require_role  # noqa: E402
from apps.api.db import make_engine, transaction  # noqa: E402
from apps.api.errors import Problem, require  # noqa: E402
from apps.api.serialization import record  # noqa: E402
from apps.api.services.common import bump_inputs, history_many  # noqa: E402
from apps.api.services.scheduling_facts import guard_goal_change  # noqa: E402
from apps.api.settings import secret_from_env  # noqa: E402
from apps.api.snapshots import scheduling_inputs  # noqa: E402
from packages.contracts.schemas import PolicyDefinition  # noqa: E402
from packages.domain.rule_lines import bound_policies  # noqa: E402
from packages.domain.rule_serialization import stored_definition  # noqa: E402
from scripts.group_rules import actor_for  # noqa: E402


def repair(db, actor, *, apply=False, reason=''):
    """Caller holds the application write lock when applying; all validation precedes writes."""
    require_role(actor, 'admin')
    identity = db.get(m.Identity, actor.identity_id)
    require(identity is not None and not identity.disabled, 'ACCESS_REVOKED', 'An enabled admin is required', 403)
    require(not apply or 3 <= len(reason.strip()) <= 400, 'REASON_REQUIRED', 'Supply a repair reason of 3 to 400 characters')
    rows = list(db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id).order_by(m.Policy.id)))
    changes = [(row, stored_definition(row.definition)) for row in rows]
    changes = [(row, definition) for row, definition in changes if definition != row.definition]
    for row, definition in changes:
        try:
            PolicyDefinition.model_validate(definition, context={'historical': True})
        except ValidationError:
            raise Problem(422, 'INVALID_RULE_DEFINITION',
                f'Rule {row.id} still fails strict validation after removing private metadata; no repairs applied') from None
    result = {'applied': apply, 'department_id': actor.department_id, 'count': len(changes),
              'rules': [{'id': row.id, 'from_revision': row.revision, 'to_revision': row.revision + 1}
                        for row, _ in changes]}
    if not changes:
        return result

    # A destination revision can also advance its reconstructed source's revision.
    replacements = {row.id: definition for row, definition in changes}
    before = [record(row) for row in rows]
    after = [{**row, 'definition': replacements[row['id']], 'revision': row['revision'] + 1}
             if row['id'] in replacements else row for row in before]
    context = scheduling_inputs(db, [actor.department_id])
    prior = {row['id']: row['revision'] for row in bound_policies(before, context)}
    affected = set(replacements) | {row['id'] for row in bound_policies(after, context)
                                   if row['revision'] != prior.get(row['id'])}
    guard_goal_change(db, actor, replacements)
    require(not db.scalar(select(m.PolicyException.id).where(
        m.PolicyException.department_id == actor.department_id, m.PolicyException.policy_id.in_(affected),
        m.PolicyException.status.in_(('proposed', 'approved')))),
        'REVISION_DEPENDENCY', 'A rule has revision-bound exceptions; reconcile their lifecycle before a metadata repair', 409)
    for workspace in db.scalars(select(m.Workspace).where(m.Workspace.department_id == actor.department_id,
            m.Workspace.status.in_(('draft', 'submitted', 'approved')))):
        require(not any(row['policy_id'] in affected for row in
                        [*(workspace.rule_adjustments or []), *(workspace.target_overrides or [])]),
            'REVISION_DEPENDENCY', 'A current rota has revision-bound adjustments; reconcile them before a metadata repair', 409)
    if not apply:
        return result

    retained = set(db.execute(select(m.DefinitionHistory.object_id, m.DefinitionHistory.object_revision).where(
        m.DefinitionHistory.department_id == actor.department_id, m.DefinitionHistory.kind == 'policy',
        m.DefinitionHistory.object_id.in_(replacements))).tuples())
    history_many(db, actor, [('policy', row, 'Original definition retained before private metadata repair')
                            for row, _ in changes if (row.id, row.revision) not in retained])
    repair_reason = 'Private rule metadata repair: ' + reason.strip()
    for row, definition in changes:
        row.definition = definition
        row.revision += 1
    history_many(db, actor, [('policy', row, repair_reason) for row, _ in changes])
    bump_inputs(db, actor)
    db.add(m.AuditEvent(department_id=actor.department_id, actor_id=actor.identity_id,
        action='policy.repair_private_metadata', target_id=None, reason=repair_reason,
        detail={'rules': result['rules']}, correlation_id=str(uuid.uuid4())))
    return result


def run(engine, who, department, *, apply=False, reason=''):
    with transaction(engine, write=apply) as db:
        return repair(db, actor_for(db, who, department), apply=apply, reason=reason)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url', default=secret_from_env('DATABASE_URL', ''))
    parser.add_argument('--as', dest='who', required=True, help='Existing enabled department admin identity')
    parser.add_argument('--department', required=True, help='Exact department id; repair never crosses this boundary')
    parser.add_argument('--apply', action='store_true', help='Append clean revisions (default: read-only dry-run)')
    parser.add_argument('--reason', default='')
    args = parser.parse_args()
    if not args.database_url:
        parser.error('Set DATABASE_URL or supply --database-url')
    engine = make_engine(args.database_url)
    try:
        try:
            result = run(engine, args.who, args.department, apply=args.apply, reason=args.reason)
        except Problem as error:
            parser.exit(2, f'{error.code}: {error.message}\n')
        print(json.dumps(result, indent=2))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
