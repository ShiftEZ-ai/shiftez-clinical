"""Store feasibility proof payloads independently of the job writer row."""
from alembic import op
import sqlalchemy as sa


revision = '0152_feasibility_results'
down_revision = '0151_job_candidate_evidence'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.create_table(
        'job_feasibility_results',
        sa.Column('job_id', sa.String(36), sa.ForeignKey('jobs.id'), primary_key=True),
        sa.Column('attempt', sa.Integer(), primary_key=True),
        sa.Column('payload', sa.JSON(), nullable=False),
    )


def downgrade():
    raise RuntimeError('Forward-only migration: retain feasibility proof payloads')
