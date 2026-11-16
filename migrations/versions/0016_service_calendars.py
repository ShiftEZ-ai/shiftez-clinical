"""Clinic mode and shared opening calendars."""
from alembic import op
import sqlalchemy as sa

revision = '0016_service_calendars'
down_revision = '0015_recorded_leave'
branch_labels = depends_on = None


def upgrade():
    op.add_column('services', sa.Column('mode', sa.String(16),
        sa.CheckConstraint("mode IN ('unspecified','physical','virtual')", name='ck_service_mode'),
        nullable=False, server_default='unspecified'))
    if op.get_bind().dialect.name == 'sqlite':
        op.execute('ALTER TABLE services ADD COLUMN calendar_id VARCHAR(36) REFERENCES operating_calendars(id)')
    else:
        op.add_column('services', sa.Column('calendar_id', sa.String(36), sa.ForeignKey('operating_calendars.id')))


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
