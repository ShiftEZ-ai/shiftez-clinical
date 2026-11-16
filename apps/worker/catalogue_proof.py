"""The strict catalogue proof runs as an ordinary leased Jobs workload."""
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import Actor, current_roles
from apps.api.errors import require
from apps.api.services.jobs import catalogue_proof_stale
from apps.api.services.workspace_access import shared_clause
from packages.domain.solver_control import Cancelled


def actor_for(db, claim):
    department = db.get(m.Department, claim['department_id'])
    identity = db.get(m.Identity, claim['requested_by'])
    return Actor(identity.id, department.id, department.organisation_id, identity.display_name,
                 current_roles(db, identity.id, department.id), identity.person_id, '')


def run(db, claim, *, progress, cancelled):
    from apps.api.services.rule_catalogue_v2 import _proof
    inputs = claim['input']
    actor = actor_for(db, claim)
    require(not catalogue_proof_stale(db, actor, inputs), 'STALE_INPUTS',
            'Conversion inputs or proof build changed; request a fresh check', 409)
    rotas = list(db.scalars(select(m.Workspace).where(m.Workspace.department_id == actor.department_id,
        shared_clause(),
        m.Workspace.status.in_(['draft', 'submitted', 'approved'])).order_by(m.Workspace.id)))
    require([rota.id for rota in rotas] == inputs['rota_ids'], 'STALE_INPUTS',
            'The current rotas changed; request a fresh check', 409)
    if cancelled():
        raise Cancelled()
    proof = _proof(db, actor, inputs['candidate'], rotas, cancelled=cancelled, progress=progress)
    if cancelled():
        raise Cancelled()
    require(not catalogue_proof_stale(db, actor, inputs), 'STALE_INPUTS',
            'Conversion inputs or proof build changed during the check; request a fresh check', 409)
    progress({'stage': 'ready', 'completed_rotas': len(rotas), 'total_rotas': len(rotas)})
    return {'status': 'identical' if proof['identical'] else 'different', 'proof': proof,
            **{key: inputs[key] for key in ('manifest', 'plan_hash', 'toolchain', 'rota_ids')}}
