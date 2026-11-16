"""Heartbeat leases and truthful job progress; old results remain readable."""
from alembic import op
import sqlalchemy as sa

revision = '0028_job_progress'
down_revision = '0027_fixed_assignments'
branch_labels = depends_on = None


def upgrade():
    op.add_column('jobs', sa.Column('heartbeat_at', sa.DateTime(timezone=True)))
    op.add_column('jobs', sa.Column('progress', sa.JSON()))
    op.execute('UPDATE jobs SET heartbeat_at = started_at WHERE status = \'running\'')


def downgrade():
    op.drop_column('jobs', 'progress')
    op.drop_column('jobs', 'heartbeat_at')
