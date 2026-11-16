"""Add per-collection personal and department default choices without rewriting saved views."""
from alembic import op
import sqlalchemy as sa

revision = "0120_table_view_defaults"
down_revision = "0120_record_insertion_order"
branch_labels = depends_on = None
SAFETY_CLASS = "additive"


def upgrade():
    op.create_table("table_view_defaults",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("identities.id"), nullable=True),
        sa.Column("table_key", sa.String(60), nullable=False),
        sa.Column("view_id", sa.String(36), sa.ForeignKey("table_views.id", ondelete="SET NULL"), nullable=True),
        sa.Column("built_in", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("NOT built_in OR view_id IS NULL"))
    op.create_index("ix_table_view_defaults_department_id", "table_view_defaults", ["department_id"])
    op.execute("CREATE UNIQUE INDEX uq_table_view_defaults_personal ON table_view_defaults (department_id, owner_id, table_key) WHERE owner_id IS NOT NULL")
    op.execute("CREATE UNIQUE INDEX uq_table_view_defaults_department ON table_view_defaults (department_id, table_key) WHERE owner_id IS NULL")


def downgrade():
    raise RuntimeError("Forward-only: retain saved-view default choices and command audit history")
