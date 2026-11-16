"""Add monitor token digests and retained, department-scoped incident evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0076_monitor_events"
down_revision = "0070_open_ended_attachments"
branch_labels = depends_on = None


def upgrade():
    op.create_table("monitor_integrations",
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), primary_key=True),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=True))
    op.create_table("monitor_incidents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("state", sa.String(4), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("event_key", sa.String(64), nullable=False),
        sa.CheckConstraint("state IN ('up','down')"),
        sa.UniqueConstraint("department_id", "event_key"))
    op.create_index("ix_monitor_incidents_department_received", "monitor_incidents", ["department_id", "received_at", "id"])


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
