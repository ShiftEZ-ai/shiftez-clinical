"""Retain disposable, scoped read projections separately from clinical evidence."""
from alembic import op
import sqlalchemy as sa

revision = '0128_workspace_read_projections'
down_revision = '0127_integration_wave1'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.create_table('workspace_read_projections',
        sa.Column('workspace_id', sa.String(36), sa.ForeignKey('workspaces.id'), primary_key=True),
        sa.Column('audience', sa.String(64), primary_key=True),
        sa.Column('kind', sa.String(32), primary_key=True),
        sa.Column('stamp', sa.String(64)), sa.Column('value', sa.JSON()))


def downgrade():
    raise RuntimeError('Forward-only: retain disposable read projections')
