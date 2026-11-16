"""Audit the inherited auto-schedule default's change from Rebalance to Keep.

Expand-only: existing settings and all recorded choices remain unchanged. Future
runs with no saved mode inherit Keep from the GenerationOptions contract.
"""
from datetime import datetime, timezone
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision = '0061_keep_assignments_default'
down_revision = '0060_quota_line_contract'
branch_labels = depends_on = None


def upgrade():
    bind = op.get_bind()
    departments = sa.table('departments', sa.column('id', sa.String()),
                           sa.column('generation_defaults', sa.JSON()))
    inherited = [row.id for row in bind.execute(sa.select(departments))
                 if 'mode' not in (row.generation_defaults or {})]
    if not inherited:
        return
    created_at = datetime.now(timezone.utc)
    actor_id = str(uuid4())
    identities = sa.table('identities', sa.column('id', sa.String()), sa.column('revision', sa.Integer()),
        sa.column('created_at', sa.DateTime(timezone=True)), sa.column('issuer', sa.String()),
        sa.column('subject', sa.String()), sa.column('display_name', sa.String()), sa.column('disabled', sa.Boolean()))
    bind.execute(identities.insert(), {'id': actor_id, 'revision': 1, 'created_at': created_at,
        'issuer': 'system-migration', 'subject': revision, 'display_name': 'Keep assignments default migration',
        'disabled': True})
    audit = sa.table('audit_events', sa.column('id', sa.String()), sa.column('revision', sa.Integer()),
        sa.column('created_at', sa.DateTime(timezone=True)), sa.column('department_id', sa.String()),
        sa.column('actor_id', sa.String()), sa.column('action', sa.String()), sa.column('target_id', sa.String()),
        sa.column('reason', sa.String()), sa.column('detail', sa.JSON()), sa.column('correlation_id', sa.String()))
    bind.execute(audit.insert(), [{'id': str(uuid4()), 'revision': 1, 'created_at': created_at,
        'department_id': department_id, 'actor_id': actor_id, 'action': 'department.generation_default',
        'target_id': department_id, 'reason': 'Owner decision: keep manual edits by default for auto-schedule',
        'detail': {'migration_revision': revision, 'field': 'generation_defaults.mode', 'before': 'rebalance',
                   'after': 'fill_gaps', 'source': 'inherited_default'}, 'correlation_id': revision}
        for department_id in inherited])


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
