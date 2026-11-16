"""Private saved views for the shared data table (layout, filters, grouping, columns), per person and list.

Revision ID: 0035_table_views
Revises: 0034_duty_colour_automatic
"""
from alembic import op
import sqlalchemy as sa

revision = "0035_table_views"
down_revision = "0034_duty_colour_automatic"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("table_views",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("table_key", sa.String(60), nullable=False),
        sa.Column("name", sa.String(160), nullable=False), sa.Column("definition", sa.JSON(), nullable=False))
    op.create_index("ix_table_views_department_id", "table_views", ["department_id"])
    op.create_index("ix_table_views_owner_id", "table_views", ["owner_id"])


def downgrade():
    raise RuntimeError("Use a reviewed forward migration; keep the normal command audit history")
