"""Record the effective date of person deactivation without rewriting history."""
from alembic import op
import sqlalchemy as sa


revision = '0150_person_inactive_from'
down_revision = '0148_bulk_analyze'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.add_column('people', sa.Column('inactive_from', sa.Date(), nullable=True))


def downgrade():
    raise RuntimeError('Forward-only migration: retain dated person inactivity history')
