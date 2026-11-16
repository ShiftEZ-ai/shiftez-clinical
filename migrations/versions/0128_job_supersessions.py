"""Each auto-schedule run records the older runs it supersedes; NULL until recorded (the worker fills existing runs)."""
from alembic import op
import sqlalchemy as sa

revision = '0128_job_supersessions'
down_revision = '0127_integration_wave1'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.add_column('jobs', sa.Column('supersedes', sa.JSON(), nullable=True))


def downgrade():
    raise RuntimeError('Forward-only: retain recorded run supersessions')
