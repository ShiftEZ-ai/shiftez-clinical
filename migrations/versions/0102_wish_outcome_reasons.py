"""Retain published wish explanations without rewriting historical outcomes."""
from alembic import op
import sqlalchemy as sa

revision = "0102_wish_outcome_reasons"
down_revision = "0101_pattern_wishes"
branch_labels = depends_on = None


def upgrade():
    op.add_column("staff_wishes", sa.Column("outcome_reasons", sa.JSON(), nullable=True))


def downgrade():
    raise RuntimeError("Forward-only: published wish explanations are part of the record")
