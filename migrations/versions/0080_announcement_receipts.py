"""Announcement receipts and separate inbox read/acknowledgement evidence.

0080 is reserved for M1; chained after batch 2's source-rota configuration.
Only the new read_at column is populated; existing acknowledgement evidence is preserved.
"""
from alembic import op
import sqlalchemy as sa

revision = "0080_announcement_receipts"
down_revision = "0074_source_rota_configuration"
branch_labels = depends_on = None


def upgrade():
    op.add_column("notifications", sa.Column("read_at", sa.DateTime(timezone=True)))
    op.add_column("notifications", sa.Column("source_kind", sa.String(40)))
    op.add_column("notifications", sa.Column("source_id", sa.String(36)))
    notes = sa.table("notifications", sa.column("read_at"), sa.column("acknowledged_at"))
    op.get_bind().execute(notes.update().where(notes.c.read_at.is_(None)).values(read_at=notes.c.acknowledged_at))
    op.create_index("ix_notifications_source_identity", "notifications", ["source_kind", "source_id", "identity_id"])
    op.add_column("departments", sa.Column("announcement_send_time", sa.String(5), nullable=False, server_default="07:00"))
    op.add_column("departments", sa.Column("announcement_auto_reminder_days", sa.Integer()))
    op.add_column("departments", sa.Column("announcement_reminder_interval_hours", sa.Integer(), nullable=False, server_default="24"))
    op.add_column("department_content", sa.Column("sent_outbox_id", sa.String(36)))
    op.add_column("department_content", sa.Column("receipts_checked_on", sa.Date()))
    op.add_column("department_content", sa.Column("last_reminded_at", sa.DateTime(timezone=True)))
    op.create_index("ix_department_content_active_dates", "department_content", ["department_id", "active", "starts_on"])
    op.create_table("announcement_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_id", sa.String(36), sa.ForeignKey("department_content.id"), nullable=False),
        sa.Column("identity_id", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("person_id", sa.String(36), sa.ForeignKey("people.id")),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("content_id", "identity_id"))
    op.create_index("ix_announcement_receipts_identity_id", "announcement_receipts", ["identity_id"])
    if op.get_bind().dialect.name == "postgresql":
        # The original migration did not add the model's importance check. Widen it where present, add it otherwise.
        for check in sa.inspect(op.get_bind()).get_check_constraints("department_content"):
            if "importance" in check["sqltext"]:
                op.drop_constraint(check["name"], "department_content", type_="check")
        op.create_check_constraint("ck_department_content_importance", "department_content",
                                   "importance IN ('normal','important','must_acknowledge')")
        op.create_foreign_key("fk_department_content_sent_outbox", "department_content", "outbox", ["sent_outbox_id"], ["id"])
        op.create_check_constraint("ck_department_announcement_auto_reminder", "departments",
                                   "announcement_auto_reminder_days IS NULL OR announcement_auto_reminder_days BETWEEN 1 AND 3650")
        op.create_check_constraint("ck_department_announcement_reminder_interval", "departments",
                                   "announcement_reminder_interval_hours BETWEEN 1 AND 8760")


def downgrade():
    raise RuntimeError("Forward-only: retain receipt and acknowledgement evidence")
