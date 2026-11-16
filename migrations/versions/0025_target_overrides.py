"""Rota-specific workload target adjustments, preserved in publication evidence."""
from alembic import op
import sqlalchemy as sa

revision = '0025_target_overrides'
down_revision = '0024_prorated_targets'
branch_labels = depends_on = None


def upgrade():
    op.add_column('workspaces', sa.Column('target_overrides', sa.JSON(), nullable=False, server_default='[]'))


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
