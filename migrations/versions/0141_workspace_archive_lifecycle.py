"""Retain the pre-archive rota lifecycle state for audited restore and delete guards.

Expand-only: nullable archive provenance/state columns; existing workspaces,
publications and audit evidence are untouched.
"""
from alembic import op
import sqlalchemy as sa

revision = "0141_workspace_archive_lifecycle"
down_revision = "0140_score_settings_versions"
branch_labels = None
depends_on = None


def upgrade():
    # Keep this nullable and constraint-free so the expand step is portable to
    # SQLite's ALTER TABLE implementation; the actor identity is audited too.
    op.add_column("workspaces", sa.Column("archived_by", sa.String(36), nullable=True))
    op.add_column("workspaces", sa.Column("archive_reason", sa.String(500), nullable=True))
    op.add_column("workspaces", sa.Column("archive_state", sa.JSON(), nullable=True))


def downgrade():
    raise RuntimeError("Forward-only: retain archive provenance and restore evidence")
