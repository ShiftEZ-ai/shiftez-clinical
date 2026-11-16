"""Retain job lease ownership across worker restarts; existing evidence stays intact."""
from alembic import op
import sqlalchemy as sa

revision = '0129_job_worker_owner'
down_revision = '0128_workspace_read_projections'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.add_column('jobs', sa.Column('worker_id', sa.String(120), nullable=True))
    op.create_index('ix_jobs_worker_lease', 'jobs', ['worker_id', 'status'])


def downgrade():
    raise RuntimeError('Forward-only: retain worker lease ownership')
