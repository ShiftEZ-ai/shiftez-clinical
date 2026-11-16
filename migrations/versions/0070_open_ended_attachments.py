"""Allow attachments without an end date, preserving existing rows and date/pairing guards."""
from alembic import op
import sqlalchemy as sa

revision = '0070_open_ended_attachments'
down_revision = '0069_schedule_view_scope'
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().dialect.name == 'sqlite':
        # SQLite needs a table copy; restate its unnamed checks so both guards survive.
        with op.batch_alter_table('mentorships', table_args=[
            sa.CheckConstraint('ends_on > starts_on'), sa.CheckConstraint('person_id <> mentor_id'),
        ]) as batch:
            batch.alter_column('ends_on', existing_type=sa.Date(), nullable=True)
    else:
        op.alter_column('mentorships', 'ends_on', existing_type=sa.Date(), nullable=True)


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
