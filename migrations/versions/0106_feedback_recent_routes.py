"""Add redacted navigation history to feedback without changing existing reports."""
from alembic import op
import sqlalchemy as sa

revision = "0106_feedback_recent_routes"
down_revision = "0099_swap_settings"
branch_labels = depends_on = None


def upgrade():
    op.add_column("feedback_reports", sa.Column("recent_routes", sa.JSON(), nullable=False, server_default="[]"))


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
