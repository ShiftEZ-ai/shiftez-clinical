"""Preserve fixed people independently of copied recurring-series links."""
from alembic import op
import sqlalchemy as sa

revision = '0027_fixed_assignments'
down_revision = '0026_rotation_types'
branch_labels = depends_on = None


def upgrade():
    op.add_column('assignments', sa.Column('fixed', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute('UPDATE assignments SET fixed = TRUE WHERE shift_id IN (SELECT id FROM shifts WHERE series_id IS NOT NULL)')


def downgrade():
    op.drop_column('assignments', 'fixed')
