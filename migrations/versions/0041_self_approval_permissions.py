"""Self-approval permissions: who may decide their own work, for which actions, between two dates.

Revision ID: 0041_self_approval_permissions
Revises: 0040_workspace_archive
"""
from alembic import op
import sqlalchemy as sa

revision = "0041_self_approval_permissions"
down_revision = "0040_workspace_archive"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("self_approval_permissions",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("identity_id", sa.String(36), sa.ForeignKey("identities.id"), nullable=True),
        sa.Column("role", sa.String(32), nullable=True),
        sa.Column("actions", sa.JSON(), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.CheckConstraint("(identity_id IS NULL) <> (role IS NULL)", name="ck_self_approval_subject"),
        sa.CheckConstraint("ends_on IS NULL OR ends_on > starts_on", name="ck_self_approval_dates"))
    op.create_index("ix_self_approval_permissions_department_id", "self_approval_permissions", ["department_id"])
    op.create_index("ix_self_approval_permissions_identity_id", "self_approval_permissions", ["identity_id"])
    # A review approval made under a permission stays valid at publication even after the permission ends.
    # SQLite table rebuilds would drop the append-only guards on review decisions; an additive REFERENCES keeps them.
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ALTER TABLE review_decisions ADD COLUMN self_approval_id VARCHAR(36) REFERENCES self_approval_permissions(id)")
    else:
        op.add_column("review_decisions", sa.Column("self_approval_id", sa.String(36), sa.ForeignKey("self_approval_permissions.id"), nullable=True))


def downgrade():
    raise RuntimeError("Use a reviewed forward migration; keep the normal command audit history")
