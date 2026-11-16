"""Bound applied-population reads to this workspace's applied generation jobs."""
from alembic import op

revision = '0135_applied_population_index'
down_revision = '0134_wie_labels'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.create_index('ix_jobs_workspace_kind_status', 'jobs', ['workspace_id', 'kind', 'status'])


def downgrade():
    raise RuntimeError('Forward-only migration: retain the applied population index')
