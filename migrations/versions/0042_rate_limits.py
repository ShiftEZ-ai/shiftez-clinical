"""Rate-limit counters for commands, exports and private calendar feeds, shared by every API worker.

Revision ID: 0042_rate_limits
Revises: 0041_self_approval_permissions
"""
from alembic import op
import sqlalchemy as sa

revision = "0042_rate_limits"
down_revision = "0041_self_approval_permissions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("rate_limits",
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_rate_limits_expires_at", "rate_limits", ["expires_at"])


def downgrade():
    op.drop_table("rate_limits")
