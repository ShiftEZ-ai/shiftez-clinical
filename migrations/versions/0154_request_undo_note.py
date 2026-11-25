"""Keep the previous waiting request note with the erasable request record."""
from alembic import op
import sqlalchemy as sa


revision = '0154_request_undo_note'
down_revision = '0153_calendar_lineage'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.add_column('requests', sa.Column('private_undo_note', sa.String(1000), nullable=True))


def downgrade():
    raise RuntimeError('Forward-only migration: retain waiting request Undo data')
