"""Programme calendar dates, independent of employee placement or leave."""
from alembic import op
import sqlalchemy as sa

revision = '0020_programme_calendar'
down_revision = '0019_source_evidence'
branch_labels = depends_on = None


def upgrade():
    op.create_table('programme_calendar_entries',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('programme_id', sa.String(36), sa.ForeignKey('programmes.id'), nullable=False),
        sa.Column('name', sa.String(160), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('kind', sa.String(24), nullable=False),
        sa.Column('starts_on', sa.Date(), nullable=False),
        sa.Column('ends_on', sa.Date(), nullable=False),
        sa.CheckConstraint('ends_on > starts_on'),
        sa.CheckConstraint("kind IN ('academic_block','break','notice')"))
    op.create_index('ix_programme_calendar_entries_programme_id', 'programme_calendar_entries', ['programme_id'])


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
