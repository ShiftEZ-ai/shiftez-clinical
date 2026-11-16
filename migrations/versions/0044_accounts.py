"""Managed sign-in accounts, one-time invitation/reset links, last sign-in, and department session limits.

Expand-only: two new tables and nullable columns.

Revision ID: 0044_accounts
Revises: 0043_announcements_workers
"""
from alembic import op
import sqlalchemy as sa

revision = "0044_accounts"
down_revision = "0043_announcements_workers"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("identities", sa.Column("last_sign_in_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("departments", sa.Column("session_idle_minutes", sa.Integer(), nullable=True))
    op.add_column("departments", sa.Column("session_max_hours", sa.Integer(), nullable=True))
    if op.get_bind().dialect.name == "postgresql":  # SQLite cannot add a table check to an existing table
        op.create_check_constraint("ck_departments_session_idle", "departments",
                                   "session_idle_minutes IS NULL OR session_idle_minutes BETWEEN 5 AND 480")
        op.create_check_constraint("ck_departments_session_max", "departments",
                                   "session_max_hours IS NULL OR session_max_hours BETWEEN 1 AND 24")
    op.create_table("accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("identity_id", sa.String(36), sa.ForeignKey("identities.id"), nullable=False, unique=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("password_changed_at", sa.DateTime(timezone=True)))
    op.create_table("account_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("identity_id", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.CheckConstraint("purpose IN ('invite','reset')"))
    op.create_index("ix_account_links_department_id", "account_links", ["department_id"])
    op.create_index("ix_account_links_identity_id", "account_links", ["identity_id"])


def downgrade():
    raise RuntimeError("Use a reviewed forward migration; account links are part of the audit trail")
