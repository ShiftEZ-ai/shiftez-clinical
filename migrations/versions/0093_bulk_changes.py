"""Retain bulk drafts and manual leave type overrides without rewriting existing data."""
from alembic import op
import sqlalchemy as sa

revision = "0093_bulk_changes"
down_revision = "0109_fair_share_dials"
branch_labels = depends_on = None


def upgrade():
    op.add_column("departments", sa.Column("bulk_settings", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("availability", sa.Column("manual_type_override", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("definition_history", sa.Column("batch_id", sa.String(36), nullable=True))
    op.create_index("ix_definition_history_batch_id", "definition_history", ["batch_id"])
    op.create_table("bulk_staged",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False, index=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("identities.id"), nullable=False, index=True),
        sa.Column("review_role", sa.String(32), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.CheckConstraint("status IN ('active','waiting','dropped','applied','expired')"))
    op.create_index("ix_bulk_staged_owner_status", "bulk_staged", ["department_id", "owner_id", "status", "expires_at"])


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
