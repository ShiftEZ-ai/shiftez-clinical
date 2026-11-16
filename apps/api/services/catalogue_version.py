"""Resolve a department's catalogue version with verified maintenance provenance."""
from sqlalchemy import select
from types import SimpleNamespace

from apps.api import models as m
from apps.api.serialization import record
from packages.domain.rule_lines import SCORING_DEFAULT_SOURCE, department_catalogue_version
from scripts.system_command import MAINTENANCE_ISSUER, MAINTENANCE_SUBJECT


def department_version(db, department_id, policies=None):
    default_actor_id = db.scalar(select(m.Identity.id).where(m.Identity.issuer == MAINTENANCE_ISSUER,
        m.Identity.subject == MAINTENANCE_SUBJECT, m.Identity.disabled.is_(True)))
    if policies is None:
        policies = dict.fromkeys([*db.scalars(select(m.Policy).where(
            m.Policy.department_id == department_id, m.Policy.status == 'active')),
            *(p for p in set(db.new) | set(db.dirty) if isinstance(p, m.Policy))])
        policies = [p for p in policies if p not in db.deleted
                    and p.department_id == department_id and p.status == 'active']
    policies = [p if isinstance(p, dict) else record(p) for p in policies]
    lineage, seen = [], {p['id'] for p in policies}
    pending = [p for p in policies if p['status'] == 'active']
    while pending:
        policy = pending.pop()
        definition = policy['definition']
        previous_id = definition.get('replaces_id')
        if (policy['kind'] not in {'balance_goal', 'block_quota'} or not policy.get('approved_by')
                or definition.get('severity') != 'advisory' or definition.get('merged_ids')
                or not previous_id or previous_id in seen
                or (policy.get('source') == SCORING_DEFAULT_SOURCE and default_actor_id
                    and policy.get('created_by') == default_actor_id == policy.get('approved_by'))):
            continue
        seen.add(previous_id)
        from .common import history_version
        version = history_version(db, SimpleNamespace(department_id=department_id), 'policy', previous_id)
        previous = version.snapshot if version is not None else None
        if previous:
            lineage.append(previous)
            if previous['kind'] == policy['kind']:
                pending.append(previous)
    return department_catalogue_version(policies, default_actor_id=default_actor_id, lineage=lineage)
