"""Allow a rota to name a preceding draft as planning context."""
from alembic import op
import sqlalchemy as sa


revision = '0159_context_draft'
down_revision = '0158_live_uniqueness'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    if op.get_bind().dialect.name == 'sqlite':
        op.execute('ALTER TABLE workspaces ADD COLUMN context_draft_id VARCHAR(36) REFERENCES workspaces(id)')
    else:
        op.add_column('workspaces', sa.Column('context_draft_id', sa.String(36),
                      sa.ForeignKey('workspaces.id'), nullable=True))
    op.create_index('ix_workspaces_context_draft_id', 'workspaces', ['context_draft_id'])


def downgrade():
    raise RuntimeError('Forward-only migration: preserve draft context links')
