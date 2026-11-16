"""Optional one-line labels and department-configured rank order; existing values remain intact."""
from alembic import op
import sqlalchemy as sa

revision = '0134_wie_labels'
down_revision = '0133_workspace_merge'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.add_column('tags', sa.Column('short_name', sa.String(40), nullable=False, server_default=''))
    op.add_column('rotation_types', sa.Column('short_name', sa.String(40), nullable=False, server_default=''))
    op.add_column('leave_types', sa.Column('short_name', sa.String(40), nullable=False, server_default=''))
    op.add_column('categories', sa.Column('sort_order', sa.Integer(), nullable=False, server_default='1000'))


def downgrade():
    raise RuntimeError('Forward-only migration: retain configured labels and order')
