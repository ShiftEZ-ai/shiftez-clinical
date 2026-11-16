"""Leased merge preparation; the ordinary handler retains final writer authority."""
from sqlalchemy import text

from apps.api import models as m
from apps.api.db import transaction
from apps.api.errors import require
from apps.api.services import planning_preparation as preparation, workspace_merge
from apps.api.services.common import scoped, expected
from packages.contracts.schemas import Command
from packages.domain.solver_control import Cancelled
from .catalogue_proof import actor_for


def execute(db, claim, plan, *, replay=False):
    actor = actor_for(db, claim)
    require(not db.get(m.Identity, actor.identity_id).disabled and actor.permits('coordinator'),
            'ACCESS_REVOKED', 'Scheduling access changed', 403)
    source = scoped(db, m.Workspace, claim['input']['source_id'], actor)
    expected(source, claim['input']['source_revision'])
    command = Command.model_validate(claim['input']['command'])
    db.info['_track_input_changes'] = replay
    preparation.bind(db, plan, replay=replay)
    result = (workspace_merge.preview if command.kind == 'workspace.merge.preview' else workspace_merge.apply)(db, actor, command)
    require(db.info['_planning_preparation']['next_check'] == len(plan['checks']), 'STALE_INPUTS',
            'Merge inputs changed; preview again', 409)
    if replay:
        from apps.api.serialization import compact_reply, json_value
        from apps.api.services.leave_disclosure import overlap_labels
        return overlap_labels(db, actor, json_value(compact_reply(result), copy_containers=False))
    return result


def run(engine, claim, *, progress, cancelled):
    reader = engine.execution_options(isolation_level='REPEATABLE READ') if engine.dialect.name == 'postgresql' else engine
    plan = {'seed': m.uid(), 'at': m.now(), 'checks': []}
    while True:
        if cancelled():
            raise Cancelled()
        progress({'stage': 'checking', 'completed_steps': len(plan['checks'])})
        try:
            with transaction(reader) as db:
                if engine.dialect.name == 'sqlite':
                    db.execute(text('BEGIN IMMEDIATE'))
                execute(db, claim, plan)
                raise preparation.RollbackPreparation()
        except preparation.ComputePreparation as pending:
            plan['checks'].append((pending.key, pending.compute()))
        except preparation.RollbackPreparation:
            break
    if cancelled():
        raise Cancelled()
    progress({'stage': 'saving', 'completed_steps': len(plan['checks'])})
    return plan
