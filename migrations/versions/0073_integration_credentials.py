"""Encrypted department integration credentials; existing records and evidence are untouched."""
from alembic import op
import sqlalchemy as sa

revision = "0073_integration_credentials"
down_revision = "0076_monitor_events"
branch_labels = depends_on = None


def upgrade():
    op.create_table("integration_credentials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False, index=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("repo", sa.String(300), nullable=False),
        sa.Column("token_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("token_hint", sa.String(4), nullable=False),
        sa.Column("connected_by", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.UniqueConstraint("department_id", "kind"),
        sa.CheckConstraint("kind = 'github_issues'"), sa.CheckConstraint("revision >= 1"))


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
