"""Add personal/department scope without changing existing saved definitions."""
from alembic import op
import sqlalchemy as sa

revision = "0069_schedule_view_scope"
down_revision = "0068_oidc_logout"
branch_labels = depends_on = None


def upgrade():
    for table in ("table_views", "planner_views"):
        op.add_column(table, sa.Column("scope", sa.String(16), nullable=False, server_default="personal"))
        op.create_index("ix_" + table + "_department_scope", table, ["department_id", "scope"])


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
