"""Advisory issues a coordinator accepted on a draft (Schedule › Resolve issues › Accept).

Expand-only: one JSON column defaulting to an empty list, added in place.

Revision ID: 0058_workspace_accepted_issues
Revises: 0057_leave_reason_display
"""
from alembic import op
import sqlalchemy as sa

revision = "0058_workspace_accepted_issues"
down_revision = "0057_leave_reason_display"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workspaces", sa.Column("accepted_issues", sa.JSON(), nullable=False, server_default="[]"))


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
