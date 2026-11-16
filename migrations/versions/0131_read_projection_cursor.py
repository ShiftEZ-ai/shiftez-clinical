"""Bound read-projection refresh discovery with a durable cursor and an ordered workspace index."""
from alembic import op
import sqlalchemy as sa

revision = '0131_read_projection_cursor'
down_revision = '0130_session_lineage'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.create_table('read_projection_warm_cursors',
        sa.Column('key', sa.String(32), primary_key=True),
        sa.Column('after_id', sa.String(36), nullable=True))
    op.create_index('ix_workspaces_archived_id', 'workspaces', ['archived_at', 'id'])


def downgrade():
    raise RuntimeError('Forward-only: retain disposable refresh cursor and index')
