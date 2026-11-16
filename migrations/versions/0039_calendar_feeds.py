"""Personal calendar subscriptions: a private, revocable link per person (only a hash of the secret is stored).

Revision ID: 0039_calendar_feeds
Revises: 0038_rota_rule_adjustments
"""
from alembic import op
import sqlalchemy as sa

revision = "0039_calendar_feeds"
down_revision = "0038_rota_rule_adjustments"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("calendar_feeds",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("identity_id", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("person_id", sa.String(36), sa.ForeignKey("people.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_calendar_feeds_identity_id", "calendar_feeds", ["identity_id"])


def downgrade():
    raise RuntimeError("Use a reviewed forward migration; keep the normal command audit history")
