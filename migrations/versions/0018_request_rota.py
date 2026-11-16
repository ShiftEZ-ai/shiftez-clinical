"""Trace approved requests to their separately reviewed rota."""
from alembic import op
import sqlalchemy as sa

revision = '0018_request_rota'
down_revision = '0017_mentorships'
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().dialect.name == 'sqlite':
        op.execute('ALTER TABLE requests ADD COLUMN workspace_id VARCHAR(36) REFERENCES workspaces(id)')
    else:
        op.add_column('requests', sa.Column('workspace_id', sa.String(36), sa.ForeignKey('workspaces.id'), nullable=True))


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
