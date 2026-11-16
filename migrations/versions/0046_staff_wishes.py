"""Staff wishes and the wish ledger (declined-wish debt).

Expand-only: two new tables.

Revision ID: 0046_staff_wishes
Revises: 0045_staff_imports
"""
from alembic import op
import sqlalchemy as sa

revision = "0046_staff_wishes"
down_revision = "0045_staff_imports"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("staff_wishes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("person_id", sa.String(36), sa.ForeignKey("people.id"), nullable=False),
        sa.Column("first_day", sa.Date(), nullable=False),
        sa.Column("last_day", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("assignment_type_id", sa.String(36), sa.ForeignKey("assignment_types.id")),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("outcome", sa.String(16)),
        sa.Column("publication_id", sa.String(36), sa.ForeignKey("publications.id")),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.CheckConstraint("last_day >= first_day"), sa.CheckConstraint("points BETWEEN 1 AND 10"),
        sa.CheckConstraint("status IN ('open','withdrawn')"),
        sa.CheckConstraint("kind IN ('prefer_off','prefer_on','prefer_shift_type','avoid_shift_type')"))
    op.create_index("ix_staff_wishes_department_id", "staff_wishes", ["department_id"])
    op.create_index("ix_staff_wishes_person_id", "staff_wishes", ["person_id"])
    op.create_index("ix_staff_wishes_department_days", "staff_wishes", ["department_id", "first_day"])
    op.create_table("wish_ledger",
        sa.Column("publication_id", sa.String(36), sa.ForeignKey("publications.id"), primary_key=True),
        sa.Column("person_id", sa.String(36), sa.ForeignKey("people.id"), primary_key=True),
        sa.Column("metric", sa.String(40), primary_key=True),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False))
    op.create_index("ix_wish_ledger_person", "wish_ledger", ["department_id", "person_id", "period_start"])


def downgrade():
    raise RuntimeError("Use a reviewed forward migration; wishes and their outcomes are part of the record")
