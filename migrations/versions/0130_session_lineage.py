"""Add clone identity without changing retained sessions or publication evidence."""
from alembic import op
import sqlalchemy as sa

revision = '0130_session_lineage'
down_revision = '0129_job_worker_owner'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.add_column('shifts', sa.Column('lineage_id', sa.String(36), nullable=True))


def downgrade():
    raise RuntimeError('Forward-only: retain session lineage')
