"""Keep private auto-schedule preparation, hints and live search controls across workers."""
from alembic import op
import sqlalchemy as sa

revision = '0111_autoschedule_resume'
down_revision = '0110_feedback_github_reservation'
branch_labels = depends_on = None


def upgrade():
    op.add_column('jobs', sa.Column('resume_state', sa.JSON(), nullable=True))
    op.add_column('jobs', sa.Column('resume_checkpoint', sa.JSON(), nullable=True))
    op.add_column('jobs', sa.Column('search_control', sa.JSON(), nullable=True))


def downgrade():
    raise RuntimeError('Forward-only: retain saved auto-schedule work')
