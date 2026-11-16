"""How leave shows on planning findings: 'none' (no reason), 'type' (type and dates) or 'details' (also the note).

Expand-only: one column defaulting to 'type', added in place.

Revision ID: 0057_leave_reason_display
Revises: 0056_department_weekend
"""
from alembic import op
import sqlalchemy as sa

revision = "0057_leave_reason_display"
down_revision = "0056_department_weekend"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("departments", sa.Column("leave_reason_display", sa.String(16), nullable=False, server_default="type"))
    if op.get_bind().dialect.name == "postgresql":  # SQLite cannot add a table check to an existing table
        op.create_check_constraint("ck_departments_leave_reason_display", "departments",
                                   "leave_reason_display IN ('none','type','details')")


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
