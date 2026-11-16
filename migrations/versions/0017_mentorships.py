"""Dated mentors independent of clinical supervision authority."""
from alembic import op
import sqlalchemy as sa

revision = '0017_mentorships'
down_revision = '0016_service_calendars'
branch_labels = depends_on = None


def upgrade():
    op.create_table('mentorships',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('person_id', sa.String(36), sa.ForeignKey('people.id'), nullable=False),
        sa.Column('mentor_id', sa.String(36), sa.ForeignKey('people.id'), nullable=False),
        sa.Column('programme_id', sa.String(36), sa.ForeignKey('programmes.id')),
        sa.Column('starts_on', sa.Date(), nullable=False),
        sa.Column('ends_on', sa.Date(), nullable=False),
        sa.CheckConstraint('ends_on > starts_on'), sa.CheckConstraint('person_id <> mentor_id'))
    op.create_index('ix_mentorships_department_id', 'mentorships', ['department_id'])
    op.create_index('ix_mentorships_person_id', 'mentorships', ['person_id'])


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
