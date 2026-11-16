"""Nullable, non-identifying session metadata and indexed person account lookup.

Expand-only: existing sessions retain unknown metadata until their next activity.
"""
from alembic import op
import sqlalchemy as sa

revision = "0062_session_devices"
down_revision = "0061_keep_assignments_default"
branch_labels = depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sessions", sa.Column("device_label", sa.String(100), nullable=True))
    op.add_column("sessions", sa.Column("network_label", sa.String(32), nullable=True))
    op.create_index("ix_identities_person_id", "identities", ["person_id"])


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
