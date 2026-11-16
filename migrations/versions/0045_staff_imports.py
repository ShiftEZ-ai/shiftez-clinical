"""Staff spreadsheet import batches (people, ranks, clinics, skills, programmes).

Expand-only: one new table.

Revision ID: 0045_staff_imports
Revises: 0044_accounts
"""
from alembic import op
import sqlalchemy as sa

revision = "0045_staff_imports"
down_revision = "0044_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("staff_import_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("source_name", sa.String(160), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("rows", sa.JSON(), nullable=False),
        sa.Column("applied", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("identities.id"), nullable=False))
    op.create_index("ix_staff_import_batches_department_id", "staff_import_batches", ["department_id"])


def downgrade():
    raise RuntimeError("Use a reviewed forward migration; import batches are part of the audit trail")
