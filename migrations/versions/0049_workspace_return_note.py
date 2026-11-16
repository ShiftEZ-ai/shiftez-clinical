"""The approver's return note on a rota: who returned it, when and why, shown on the draft until it is submitted again.

Expand-only: one nullable column.

Revision ID: 0049_workspace_return_note
Revises: 0048_fairness_location_keys
"""
from alembic import op
import sqlalchemy as sa

revision = "0049_workspace_return_note"
down_revision = "0048_fairness_location_keys"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workspaces", sa.Column("return_note", sa.JSON(), nullable=True))


def downgrade():
    raise RuntimeError("Use a reviewed forward migration; return notes are part of the review record")
