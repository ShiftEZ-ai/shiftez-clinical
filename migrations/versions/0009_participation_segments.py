"""Add explicit planned-presence segments without rewriting issued evidence.

A participant still owns ONE duty ID and reserves their full start/end envelope.
The empty array preserves every earlier contiguous-duty interpretation.
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_participation_segments"
down_revision = "0008_series_lineage"
branch_labels = depends_on = None


def upgrade():
    # Additive on both engines. No table rebuild, dropped guards or snapshot edits.
    op.add_column("assignments", sa.Column("segments", sa.JSON(), nullable=False, server_default="[]"))


def downgrade():
    raise RuntimeError("Forward-only: do not discard planned participation segments")
