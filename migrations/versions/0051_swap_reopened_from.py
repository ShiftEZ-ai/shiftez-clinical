"""A swap reopened by Undo links to the swap it continues, so its history reads as one thread.

Undo never moves a swap backwards (the 0005 consent guard keeps status and consent forward-only); it closes the swap
forward and inserts a new one at the earlier stage, carrying the consent valid then. Expand-only: one nullable column,
added in place (no table rebuild, so the SQLite guard triggers stay as they are).

Revision ID: 0051_swap_reopened_from
Revises: 0050_publication_effective_range
"""
from alembic import op
import sqlalchemy as sa

revision = "0051_swap_reopened_from"
down_revision = "0050_publication_effective_range"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "sqlite":  # ALTER in place: a batch rebuild would drop the guard triggers
        op.execute("ALTER TABLE swap_requests ADD COLUMN reopened_from_id VARCHAR(36) REFERENCES swap_requests(id)")
    else:
        op.add_column("swap_requests", sa.Column("reopened_from_id", sa.String(36), sa.ForeignKey("swap_requests.id"), nullable=True))


def downgrade():
    raise RuntimeError("Use a reviewed forward migration; reopened swaps are part of the consent record")
