"""Reusable opening calendars and department-scoped resource pools.

Revision ID: 0007_operational_resources
Revises: 0006_planner_views
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_operational_resources"
down_revision = "0006_planner_views"
branch_labels = None
depends_on = None


def common():
    return [sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
            sa.Column("name", sa.String(160), nullable=False), sa.Column("description", sa.Text(), nullable=False),
            sa.Column("starts_on", sa.Date(), nullable=False), sa.Column("ends_on", sa.Date()),
            sa.Column("active", sa.Boolean(), nullable=False)]


def upgrade():
    op.create_table("operating_calendars", *common(), sa.Column("definition", sa.JSON(), nullable=False),
                    sa.CheckConstraint("ends_on IS NULL OR ends_on > starts_on"))
    op.create_index("ix_operating_calendars_department_id", "operating_calendars", ["department_id"])
    # A nullable inline FK is additive on SQLite; no rebuild of the existing
    # guarded locations/publication tables or historical snapshots is required.
    if op.get_bind().dialect.name == "sqlite":
        # SQLite supports an inline nullable REFERENCES column. Alembic's generic
        # add_column splits the FK into unsupported ALTER ADD CONSTRAINT.
        op.execute("ALTER TABLE locations ADD COLUMN calendar_id VARCHAR(36) REFERENCES operating_calendars(id)")
    else:
        op.add_column("locations", sa.Column("calendar_id", sa.String(36), sa.ForeignKey("operating_calendars.id")))
    op.create_table("resource_pools", *common(),
                    sa.Column("capacity", sa.Integer(), nullable=False),
                    sa.Column("location_id", sa.String(36), sa.ForeignKey("locations.id")),
                    sa.Column("calendar_id", sa.String(36), sa.ForeignKey("operating_calendars.id")),
                    sa.CheckConstraint("capacity > 0 AND capacity <= 1000"),
                    sa.CheckConstraint("ends_on IS NULL OR ends_on > starts_on"))
    op.create_index("ix_resource_pools_department_id", "resource_pools", ["department_id"])


def downgrade():
    raise RuntimeError("Use a reviewed forward migration; do not discard referenced resources or calendar history")
