"""Store retained generation evidence beside its owning job."""
from alembic import op
import sqlalchemy as sa


revision = '0151_job_candidate_evidence'
down_revision = '0150_person_inactive_from'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.create_table(
        'job_candidate_evidence',
        sa.Column('job_id', sa.String(36), sa.ForeignKey('jobs.id'), primary_key=True),
        sa.Column('ordinal', sa.Integer(), primary_key=True),
        sa.Column('shift_id', sa.String(36), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=False),
    )
    op.create_index('ix_job_candidate_evidence_shift', 'job_candidate_evidence',
                    ['job_id', 'shift_id', 'ordinal'])


def downgrade():
    raise RuntimeError('Forward-only migration: retain job candidate evidence')
