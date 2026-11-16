"""Private server-owned planner filters, not clinical configuration.

Revision ID: 0006_planner_views
Revises: 0005_governance_guards
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_planner_views"
down_revision = "0005_governance_guards"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("planner_views",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("name", sa.String(160), nullable=False), sa.Column("definition", sa.JSON(), nullable=False))
    op.create_index("ix_planner_views_department_id", "planner_views", ["department_id"])
    op.create_index("ix_planner_views_owner_id", "planner_views", ["owner_id"])


def downgrade():
    raise RuntimeError("Use a reviewed forward migration; keep the normal command audit history")
