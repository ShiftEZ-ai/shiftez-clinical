"""Weekly availability patterns (recurring unavailability), proposed by staff and approved by coordinators.

Revision ID: 0037_availability_patterns
Revises: 0036_planner_saved_views
"""
from alembic import op
import sqlalchemy as sa

revision = "0037_availability_patterns"
down_revision = "0036_planner_saved_views"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("availability_patterns",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("person_id", sa.String(36), sa.ForeignKey("people.id"), nullable=False),
        sa.Column("weekdays", sa.JSON(), nullable=False),
        sa.Column("start_time", sa.String(5), nullable=False), sa.Column("end_time", sa.String(5), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False), sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("decided_by", sa.String(36), sa.ForeignKey("identities.id"), nullable=True))
    op.create_index("ix_availability_patterns_department_id", "availability_patterns", ["department_id"])
    op.create_index("ix_availability_patterns_person_id", "availability_patterns", ["person_id"])


def downgrade():
    raise RuntimeError("Use a reviewed forward migration; keep the normal command audit history")
