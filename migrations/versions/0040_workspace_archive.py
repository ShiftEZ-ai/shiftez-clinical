"""A draft rota can be discarded (archived) and restored; archived drafts leave lists, counts and rule checks.

Revision ID: 0040_workspace_archive
Revises: 0039_calendar_feeds
"""
from alembic import op
import sqlalchemy as sa

revision = "0040_workspace_archive"
down_revision = "0039_calendar_feeds"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workspaces", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    raise RuntimeError("Use a reviewed forward migration; keep the normal command audit history")
