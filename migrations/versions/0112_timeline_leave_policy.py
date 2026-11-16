"""Add rotation-type leave policy without changing existing rota or approval evidence."""
from alembic import op
import sqlalchemy as sa

revision = '0112_timeline_leave_policy'
down_revision = '0111_autoschedule_resume'
branch_labels = depends_on = None


def upgrade():
    op.add_column('rotation_types', sa.Column('leave_policy', sa.String(24),
        sa.CheckConstraint("leave_policy IN ('not_allowed','needs_approval','allowed')"), nullable=False,
        server_default='not_allowed'))


def downgrade():
    raise RuntimeError('Forward-only: retain rotation policy and approval evidence')
