"""Select a department operating calendar without inferring or rewriting existing settings."""
from alembic import op
import sqlalchemy as sa

revision = "0113_department_quota_calendar"
down_revision = "0112_timeline_leave_policy"
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ALTER TABLE departments ADD COLUMN operating_calendar_id VARCHAR(36) REFERENCES operating_calendars(id)")
    else:
        op.add_column("departments", sa.Column("operating_calendar_id", sa.String(36),
                      sa.ForeignKey("operating_calendars.id"), nullable=True))


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
